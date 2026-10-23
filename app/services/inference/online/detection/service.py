"""模型推理服务：装配取帧调度 + 推理子进程代理 + 写回。

职责：
- 装配 StageAwareDispatcher（取帧 + 组批 + 直接提交子进程，单提交者，无独立 submit 线程）
- 装配 RemoteInferProxy（GPU 前向在独立子进程，独占 GIL）
- 提供 _write_back_results：collector（在 proxy 内）据 req_id 组装 FrameDetection 后落回该 run 的 session
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.types.detection import FrameDetection
from app.services.client.config import get_client_config
from app.services.inference.online.topics import DETECTION_WINDOW, LATEST_DETECTION
from app.services.session.instance import session_service
from app.services.session.topics import DETECTIONS
from app.services.utils.startup_latency import log_milestone
from .dispatcher import StageAwareDispatcher
from .infer_proxy import RemoteInferProxy
from app.settings import settings
from app.services.utils.metrics import frame_drop_total

logger = logging.getLogger(__name__)

# 在途批数上限（背压 + 防 pending 无界）。1~4 路 + 少数 stage 下 8 足够；GPU 本就串行，
# 过大只增内存与延迟。真需调优再上 settings（当前无该旋钮，YAGNI）。
DEFAULT_MAX_INFLIGHT = 8


class DetectionService:
    """检测服务：装配取帧调度 + 推理子进程代理 + 写回（单提交者，无独立 submit 线程）。

    职责：
    - 装配 StageAwareDispatcher（取帧 + 组批 + 直接提交子进程，唯一提交者）
    - 装配 RemoteInferProxy（GPU 前向在独立子进程）
    - collector（proxy 内）据 req_id 组装 FrameDetection，经 _write_back_results 落回该 run 的 session
    """

    def __init__(
        self,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        max_batch_per_stage: int = 8,
        sessions=None,
    ):
        """
        Args:
            stage_configs: Stage 配置（完全解耦版本，使用 InferenceWorkflow）
                {
                    "LEAK": {
                        "models": [bubble_task, bending_task],  # InferenceWorkflow 实例列表
                        "batch_size": 4,
                    },
                    "CLEAN": {
                        "models": [quality_task],  # InferenceWorkflow 实例列表
                        "batch_size": 6,
                    },
                }
            max_batch_per_stage: 每个 stage 最大 batch 大小
            sessions: run 注册表（提供 `snapshot()`；缺省取 session_service 单例）。只交给
                Dispatcher 枚举活跃 run；写回不经它反查，走 frame.sess 捕获句柄。
        """
        sessions = sessions if sessions is not None else session_service

        # Stage 配置（必须提供）
        if stage_configs is None:
            raise ValueError("stage_configs 不能为 None，请提供 Stage 配置")
        self.stage_configs = stage_configs

        self.max_batch_per_stage = max_batch_per_stage

        # 有 detector 的 stage 主键（= 需在子进程建 pool 的 stage）。
        # 注：GPU 推理已拆进程，主进程**不再**建 StageWorker；stage_configs["models"]
        # 里的 detector 实例仅供 viz 的 prepare_visualization_data（CPU，永不加载模型），故主进程
        # 无 CUDA context（时序 GRU 用 torch 但钉 CPU）。子进程用同一 StageFactory 代码路径从
        # YAML 自建自己的 pool + 加载权重。
        self._active_stages: List[str] = [
            stage for stage, cfg in self.stage_configs.items() if cfg.get("models")
        ]
        # 每 stage 组批上限（dispatcher 据此从各 stage deque 拉批）
        stage_batch_sizes = {
            stage: cfg.get("batch_size", max_batch_per_stage)
            for stage, cfg in self.stage_configs.items()
        }

        # 推理子进程代理：submit 批帧 → 子进程 _infer_models → collector 据 req_id 重组
        # FrameDetection 走 _write_back_results 落回主链路。写回回调注入本服务的单一写回口。
        # 先于 Dispatcher 构造：dispatcher 需注入它的 submit 作为唯一提交者。
        # 送帧走共享内存环：单槽 = CQ 声明的帧尺寸，槽位数 = 在途批上限 × 最大批大小（环不先于在途上限耗尽）。
        frame_cfg = get_client_config().frame
        max_batch = max(stage_batch_sizes.values(), default=max_batch_per_stage)
        self._proxy = RemoteInferProxy(
            active_stages=self._active_stages,
            write_back=self._write_back_results,
            max_inflight=DEFAULT_MAX_INFLIGHT,
            cuda_device=settings.cuda_device,
            frame_slot_bytes=frame_cfg.resize_width * frame_cfg.resize_height * 3,
            ring_slots=DEFAULT_MAX_INFLIGHT * max_batch,
        )

        # Dispatcher：取帧 + 组批 + 直接提交（单提交者，无独立 submit 线程）。仅注入 proxy 的
        # submit —— peek-commit 轮转排空，接了才移除、被拒即停（帧留 deque）；限流由 proxy 的
        # submit 布尔背压独管，dispatcher 不预读在途额度、不感知 inflight。
        self.dispatcher = StageAwareDispatcher(
            max_batch_per_stage=max_batch_per_stage,
            sessions=sessions,
            active_stages=self._active_stages,
            stage_batch_sizes=stage_batch_sizes,
            submit_batch=self._proxy.submit,
        )

        logger.info(
            "DetectionService initialized: stages=%s", self._active_stages
        )

    def start(self):
        """启动服务：推理子进程（含 warmup + 就绪屏障）→ Dispatcher（取帧+组批+提交一体）。"""
        # 先起子进程并等就绪（内部 warmup 触发模型加载 + CUDA init），避免首帧撞加载。
        self._proxy.start()
        # Dispatcher 单线程即完成取帧→组批→提交；不再有 per-stage 提交线程。
        self.dispatcher.start()
        logger.info("DetectionService started (single-dispatcher submit)")

    def stop(self):
        """停止服务：停 Dispatcher（取帧+提交都在它单线程里）→ 停子进程代理（排空在途 + 杀子进程）。"""
        # 先停 dispatcher：停后不再有新 submit（取帧与提交同在其单线程）。
        self.dispatcher.stop()

        # 再停代理：内部先排空在途批（collector 写回把检测结果放进 DETECTIONS），再杀子进程。排空出来
        # 的这批还要有人拉走才算不丢——靠 main.py 把 recording.lifespan 嵌在 inference 外层：
        # 本服务停完、run_control 交出残余，recording 的队列那时还活着。
        # CUDA wedge 现在是子进程的事：卡死的是子进程，代理直接 kill 重启，主线程不再被 daemon 强杀。
        self._proxy.stop()

        logger.info("DetectionService stopped")

    def _write_back_results(self, results: List[FrameDetection]):
        """将推理结果写到**捕获的 session 句柄**（frame.sess），不按 task_id 反查。

        三写：DETECTION_WINDOW（帧窗，供时序 actor）、LATEST_DETECTION（原子快照，供 viz）、
        DETECTIONS（落盘缓冲，供 recording）。

        句柄化写回：dispatcher pop 帧时捕获该 run 的 session 随 batch 同行，此处直接写它。
        dispatch→infer→write-back 期间若 stop_run 换代，旧 session 已 DRAINING/CLOSED，本处
        is_active() 门提前挡住三写，迟到结果落 stale_run 计数而**碰不到新 run**。各 channel
        自身也有 ACTIVE 写门，顶层这道只是提前退出 + 计数。

        **本方法不碰盘**：落盘缓冲交给 recording 的 sweeper 拉走，写进该 run 的目录。
        """
        for frame in results:
            # 取走句柄并置空：同一对象随后进帧窗 / 快照 / 落盘缓冲，留存的帧一律不带 sess
            # （不成 sess → 帧窗 → 帧 → sess 的引用环，下游也读不到可能已过期的句柄）。
            sess, frame.sess = frame.sess, None
            run = sess.info.run
            if not sess.is_active():
                # 迟到结果：捕获的 run 已被拆除/结算（DRAINING/CLOSED），整条丢弃并计数。
                frame_drop_total.labels(reason="stale_run").inc()
                logger.debug(
                    "[Worker] Skip write-back for stale run: task=%s state=%s",
                    run.task_id, sess.state.name,
                )
                continue

            # 失败可见性：pool 把模型异常降级成 success=False 的空结果（画面照常，只是没框）。
            # 此处 run 已是确定单值（句柄按帧拆开），按 run 记一条 warning，
            # 聚合计数由 pool.infer_failure_total 承接、此处不再重复计数。
            degraded = False
            for task_name, detection_output in frame.by_source.items():
                if not detection_output.success:
                    degraded = True
                    logger.warning(
                        "[Worker] inference degraded (empty result): task=%s model=%s error=%s",
                        run.task_id, task_name, detection_output.error,
                    )
            # 帧窗 / 原子快照 / 落盘缓冲共用 collector 组装的这一份（pool 每帧新建、无别名突变），不复制。
            # Path 1: 帧窗（temporal 需要历史窗口）——一帧一条 push。
            sess.channel(DETECTION_WINDOW).push(frame)
            # Path 2: 原子快照（visualization 只需最新，保证所有 task 同帧一致；无 sess，不成自引用环）
            latest = sess.channel(LATEST_DETECTION)
            first = latest.get() is None
            if latest.set(frame) and first:
                # 启动延迟埋点 B：本 run 首个推理结果写回（写回线程是 LATEST_DETECTION 唯一写方）
                log_milestone(
                    "first_inference", task_id=run.task_id, step_id=run.step_id,
                    started_at=sess.info.started_at,
                )
            # Path 3: 落盘缓冲（offline 链路硬需求）——本线程不碰盘，只入 DETECTIONS，
            # 由 recording 的 sweeper 每 tick 拉走写 detections.jsonl。落的是同一份帧级
            # FrameDetection（与帧窗/快照共用一个对象）。
            # 降级帧不落：落盘格式不带 success，空框会被离线当成「没检出」。失败时段在离线侧
            # 是时间空洞；整个 run 全失败则该 step 无检测结果（离线 skipped）。
            if not degraded:
                sess.channel(DETECTIONS).push(frame)

