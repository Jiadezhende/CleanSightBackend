"""模型推理服务：编排取帧分组 + 提交到推理子进程 + 写回。

职责：
- 管理 StageAwareDispatcher（取帧分组）
- 每 stage 起提交线程：组批 → RemoteInferProxy.submit（GPU 前向在独立子进程，独占 GIL）
- collector（在 proxy 内）据 req_id 重组 FrameInference，经 _write_back_results 落回 ClientQueues
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from app.domain.detection import FrameFeature
from app.services.client import ClientManager, client_manager
from app.services.inference.detection.dispatcher import StageAwareDispatcher
from app.services.inference.models import FrameInference
from app.services.inference.detection.remote_infer import RemoteInferProxy
from app.utils.exceptions import AppError
from app.utils.metrics import frame_drop_total
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)

# 组批等待窗口（ms）。供给由 dispatcher 10ms 轮询量化、每轮每客户端仅取 1 帧，
# 1~3ms 窗口内等不到下一轮到帧，故 timeout 的具体值对结果惰性——组批收益来自
# 队列积压而非等待。此前按 queue_depth 分 1/2/3ms 三档是死代码，拍平为固定值。
BATCH_TIMEOUT_MS = 2.0

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

        # 创建 Dispatcher（直接引用 ClientManager）
        self.dispatcher = StageAwareDispatcher(
            max_batch_per_stage=max_batch_per_stage,
            client_manager_instance=self._client_manager,
        )

        # 有 detector 的 stage 主键（= 需在子进程建 pool 的 stage）。
        # 注：GPU 推理已拆进程，主进程**不再**建 MultiModelWorkerPool；stage_configs["models"]
        # 里的 detector 实例仅供 viz 的 prepare_visualization_data（CPU，永不加载模型），故主进程
        # 保持 CUDA-free。子进程用同一 StageFactory 代码路径从 YAML 自建自己的 pool + 加载权重。
        self._active_stages: List[str] = [
            stage for stage, cfg in self.stage_configs.items() if cfg.get("models")
        ]

        # 推理子进程代理：submit 批帧 → 子进程 _infer_models → collector 据 req_id 重组
        # FrameInference 走 _write_back_results 落回主链路。写回回调注入本服务的单一写回口。
        self._proxy = RemoteInferProxy(
            active_stages=self._active_stages,
            write_back=self._write_back_results,
            max_inflight=DEFAULT_MAX_INFLIGHT,
        )

        self._stop_event = threading.Event()
        self._worker_threads: List[threading.Thread] = []

        logger.info(
            f"ModelWorkerService initialized: stages={self._active_stages}"
        )

    def start(self):
        """启动服务：推理子进程（含 warmup + 就绪屏障）→ Dispatcher → 每 stage 提交线程。"""
        # 先起子进程并等就绪（内部 warmup 触发模型加载 + CUDA init），避免首帧撞加载。
        self._proxy.start()

        self.dispatcher.start()

        # 为每个 stage 启动一个提交线程（batch → proxy.submit，非阻塞、不再持 GPU）
        for stage in self._active_stages:
            from functools import partial
            thread = threading.Thread(
                target=guarded_run,
                args=(partial(self._inference_loop, stage), self._stop_event, f"InferWorker-{stage}"),
                daemon=True,
                name=f"InferWorker-{stage}",
            )
            thread.start()
            self._worker_threads.append(thread)

        logger.info(f"Started {len(self._worker_threads)} inference submit threads")

    def stop(self):
        """停止服务：停提交 → 停 dispatcher → 停子进程代理（排空在途写回 + 杀子进程）。"""
        self._stop_event.set()
        self.dispatcher.stop()

        for thread in self._worker_threads:
            # 提交线程现在只做 batch→submit（不再持 GPU），join 应立即返回，不会 CUDA wedge。
            thread.join(timeout=2.0)
            if thread.is_alive():
                logger.warning(
                    "[ModelWorkerService] %s 未在 2s 内退出，将被 daemon 强杀", thread.name,
                )

        # 代理内部先排空在途批（collector 写回落 FeatureStore），再杀子进程——排空-先于-flush
        # 的不丢数据不变式由 InferenceManager.stop 的顺序（本 stop 早于 feature_store.flush）保证。
        # CUDA wedge 现在是子进程的事：卡死的是子进程，代理直接 kill 重启，主线程不再被 daemon 强杀。
        self._proxy.stop()

        logger.info("ModelWorkerService stopped")

    def _inference_loop(self, stage: str):
        """提交循环：组批 → proxy.submit（非阻塞）。GPU 前向在子进程，写回由 collector 异步落。"""
        batch_size = self.stage_configs[stage].get(
            "batch_size", self.max_batch_per_stage
        )

        while not self._stop_event.is_set():
            try:
                # 队列深度仅供下方 DEBUG 日志观测（不再用于自适应超时，见 BATCH_TIMEOUT_MS）
                queue_depth = self.dispatcher.queue_depth(stage)

                # 从 Dispatcher 获取该 stage 的 batch（固定等待窗口）
                batch = self.dispatcher.get_batch_for_stage(
                    stage, max_size=batch_size, timeout_ms=BATCH_TIMEOUT_MS
                )

                if not batch:
                    # 没有请求，短暂休眠
                    time.sleep(0.01)
                    continue

                # 异步提交到子进程；返回 False = 在途满/子进程未就绪 → 整批丢弃并计数
                # （背压兜底，防 pending 无界；丢帧非损坏，与既有 backpressure 同性质）。
                accepted = self._proxy.submit(batch)
                if not accepted:
                    frame_drop_total.labels(reason="infer_inflight_full").inc(len(batch))

                if logger.isEnabledFor(logging.DEBUG):
                    logger.debug(
                        "[Worker-%s] Batch submitted: size=%d/%d, accepted=%s, queue_depth=%d",
                        stage, len(batch), batch_size, accepted, queue_depth,
                    )

            # 注：不为模型推理异常单列 except——模型异常在子进程 _infer_models 就地降级为
            # FrameDetections(success=False)（batch 跨多 run，不能上抛炸整批），经响应回主进程；
            # 失败可见性由 collector 发 infer_failure_total 承接。下面 except AppError 仅兜底
            # 组批/提交路径万一抛出的应用异常。
            except AppError as e:
                # 边界层 1: 应用异常 - ERROR级别
                logger.error(
                    "[BoundaryLayer1][Worker-%s] Application error: %s", stage, e,
                    exc_info=True,
                    extra={"task_id": getattr(e, "task_id", None)},
                )
                time.sleep(0.1)
                continue

            except Exception as e:
                # 边界层 1: 未预期的异常 - CRITICAL级别
                logger.critical(
                    "[BoundaryLayer1][Worker-%s] Unexpected error: %s", stage, e,
                    exc_info=True,
                )
                time.sleep(0.5)
                continue

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

