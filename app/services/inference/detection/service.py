"""模型推理服务：装配取帧调度 + 推理子进程代理 + 写回。

职责：
- 装配 StageAwareDispatcher（取帧 + 组批 + 直接提交子进程，单提交者，无独立 submit 线程）
- 装配 RemoteInferProxy（GPU 前向在独立子进程，独占 GIL）
- 提供 _write_back_results：collector（在 proxy 内）据 req_id 重组 FrameInference 后落回 ClientQueues
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from app.domain.detection import FrameFeature
from app.services.client import ClientManager, client_manager
from app.services.inference.detection.dispatcher import StageAwareDispatcher
from app.services.inference.models import FrameInference
from app.services.inference.detection.infer_proxy import RemoteInferProxy
from app.utils.metrics import frame_drop_total

logger = logging.getLogger(__name__)

# 在途批数上限（背压 + 防 pending 无界）。1~4 路 + 少数 stage 下 8 足够；GPU 本就串行，
# 过大只增内存与延迟。真需调优再上 settings（当前无该旋钮，YAGNI）。
DEFAULT_MAX_INFLIGHT = 8


class ModelWorkerService:
    """模型推理服务：编排取帧分组 + 提交推理子进程 + 写回。

    职责：
    - 管理 StageAwareDispatcher（取帧分组）
    - 每 stage 起提交线程：组批 → RemoteInferProxy.submit（GPU 前向在独立子进程）
    - collector 据 req_id 重组 FrameInference，经 _write_back_results 同步到 ClientQueues.slide_window
    """

    def __init__(
        self,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        max_batch_per_stage: int = 8,
        client_manager_instance: Optional[ClientManager] = None,
        feature_store: Optional[Any] = None,
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
            client_manager_instance: ClientManager 实例（仅用于构造 Dispatcher 枚举 registry；
                写回不再经它反查，改走 res.cq 捕获句柄）
        """

        # 保存 ClientManager 实例：仅供 Dispatcher 枚举活跃 run（合法 multi-run 调度）；
        # 写回路径已句柄化（res.cq），不再经此反查 —— 见 _write_back_results。
        self._client_manager = client_manager_instance or client_manager

        # L2 特征落盘（常开；离线链路硬需求）。由 InferenceManager 注入并管理生命周期
        self._feature_store = feature_store

        # Stage 配置（必须提供）
        if stage_configs is None:
            raise ValueError("stage_configs 不能为 None，请提供 Stage 配置")
        self.stage_configs = stage_configs

        self.max_batch_per_stage = max_batch_per_stage

        # 有 detector 的 stage 主键（= 需在子进程建 pool 的 stage）。
        # 注：GPU 推理已拆进程，主进程**不再**建 MultiModelWorkerPool；stage_configs["models"]
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
        # FrameInference 走 _write_back_results 落回主链路。写回回调注入本服务的单一写回口。
        # 先于 Dispatcher 构造：dispatcher 需注入它的 submit/capacity 作为唯一提交者。
        self._proxy = RemoteInferProxy(
            active_stages=self._active_stages,
            write_back=self._write_back_results,
            max_inflight=DEFAULT_MAX_INFLIGHT,
        )

        # Dispatcher：取帧 + 组批 + 直接提交（单提交者，无独立 submit 线程）。注入 proxy 的
        # submit/capacity —— 每轮先读在途额度、再按额度从各 stage deque 拉批 submit，令过量
        # 提交无竞态、不产生假丢帧。
        self.dispatcher = StageAwareDispatcher(
            max_batch_per_stage=max_batch_per_stage,
            client_manager_instance=self._client_manager,
            active_stages=self._active_stages,
            stage_batch_sizes=stage_batch_sizes,
            submit_batch=self._proxy.submit,
            capacity=self._proxy.capacity,
        )

        logger.info(
            "ModelWorkerService initialized: stages=%s", self._active_stages
        )

    def start(self):
        """启动服务：推理子进程（含 warmup + 就绪屏障）→ Dispatcher（取帧+组批+提交一体）。"""
        # 先起子进程并等就绪（内部 warmup 触发模型加载 + CUDA init），避免首帧撞加载。
        self._proxy.start()
        # Dispatcher 单线程即完成取帧→组批→提交；不再有 per-stage 提交线程。
        self.dispatcher.start()
        logger.info("ModelWorkerService started (single-dispatcher submit)")

    def stop(self):
        """停止服务：停 Dispatcher（取帧+提交都在它单线程里）→ 停子进程代理（排空在途 + 杀子进程）。"""
        # 先停 dispatcher：停后不再有新 submit（取帧与提交同在其单线程）。
        self.dispatcher.stop()

        # 再停代理：内部先排空在途批（collector 写回落 FeatureStore），再杀子进程——排空-先于-flush
        # 的不丢数据不变式由 InferenceManager.stop 的顺序（本 stop 早于 feature_store.flush）保证。
        # CUDA wedge 现在是子进程的事：卡死的是子进程，代理直接 kill 重启，主线程不再被 daemon 强杀。
        self._proxy.stop()

        logger.info("ModelWorkerService stopped")

    def _write_back_results(self, results: List[FrameInference]):
        """将推理结果双写到**捕获的 CQ 句柄**（res.cq），不按 client_id 反查。

        双写策略：
        - slide_window（per-task 拆分）：供 TemporalWorker 历史窗口分析
        - latest_inference（原子快照）：供 VisualizationWorker 直接读取，保证同帧一致性

        句柄化写回：dispatcher pop 帧时捕获该 run 的 CQ 句柄随 batch 同行，此处直接写它。
        dispatch→infer→write-back 期间若 set_task/stop_run 换槽，旧句柄已转 DRAINING/CLOSED
        （T2 状态机），本处 is_active() 门统一挡住三写（含 FeatureStore 这条外部落盘腿——
        push_detection/set_latest_inference 虽已内建 ACTIVE 门，但 feature_store.append 不受
        cq 门约束），迟到结果落 stale_run 计数而**碰不到新 run**，无跨 run 串台。
        """
        for res in results:
            cq = res.cq
            if not cq.is_active():
                # 迟到结果：捕获的 run 已被拆除/结算（DRAINING/CLOSED），整条丢弃并计数。
                frame_drop_total.labels(reason="stale_run").inc()
                logger.debug(
                    "[Worker] Skip write-back for stale run: task=%s state=%s",
                    res.task_id, cq.get_state().name,
                )
                continue

            # 失败可见性：pool 把模型异常降级成 success=False 的空结果，下游与「真没检到」
            # 不可分。此处 res.task_id 已是确定单值（句柄按帧拆开），按 run 记一条 warning，
            # 聚合计数由 pool.infer_failure_total 承接、此处不再重复计数。
            for task_name, detection_output in res.detections.items():
                if not detection_output.success:
                    logger.warning(
                        "[Worker] inference degraded (empty result): task=%s model=%s error=%s",
                        res.task_id, task_name, detection_output.error,
                    )
            # 物化一次帧级 FrameFeature（多流已在 res.detections 内对齐）：帧窗 + 原子快照共用一份。
            # by_source 直接共享 res.detections 引用（pool 每帧新建、无别名突变），不复制。
            feature = FrameFeature(
                ts=res.timestamp, by_source=res.detections,
                frame_width=res.frame_width, frame_height=res.frame_height,
            )
            # Path 1: 帧窗（temporal 需要历史窗口）——一帧一条 push。
            cq.push_detection(feature)
            # Path 2: 原子快照（visualization 只需最新，保证所有 task 同帧一致；无 cq，不成自引用环）
            cq.set_latest_inference(feature)
            # 启动延迟埋点 B：该 run 首个推理结果写回（幂等，仅首帧触发）
            cq.mark_startup_milestone("first_inference")
            # L2 特征落盘（常开）：offline 链路硬需求，best-effort 不影响主链路。
            # 目录键 (task_id, step_id) 与 HLS 同款；任一为 None 则跳过（拒落，同 HLS 口径）。
            # 从同一句柄派生 task_id/step_id（消除跨 snapshot 二次读的键错配窗口）。
            # owner=cq：feature_store 无状态门，靠 store 内归属校验挡「顶层 is_active()
            # 通过后中途 supersede」的迟到写（分区键跨 run 共享，比 is_active() 更本质）。
            # 落盘同一份帧级 FrameFeature（与帧窗/快照共用），append/load 两端货币一致。
            if self._feature_store is not None:
                task_id = cq.task_id
                step_id = cq.step_id
                if task_id is not None and step_id is not None:
                    self._feature_store.append(task_id, step_id, feature, owner=cq)

