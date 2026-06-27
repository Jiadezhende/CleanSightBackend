"""模型推理服务：统一管理多个 stage 的 ModelWorkerPool。

职责：
- 管理 StageAwareDispatcher（取帧分组）
- 为每个 stage 创建 MultiModelWorkerPool
- 启动推理线程，消费各 stage 的批量请求
- 将 FrameDetections 同步到 ClientQueues.slide_window
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from app.services.client import ClientManager, ClientQueues, client_manager
from app.services.inference.core.dispatcher import StageAwareDispatcher
from app.services.inference.models import FrameInference
from app.services.inference.workers.base import MultiModelWorkerPool
from app.utils.exceptions import (
    AppError,
    FrameDrop,
    ModelInferenceError,
)
from app.utils.metrics import gpu_oom_total
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


class ModelWorkerService:
    """模型推理服务：统一管理多个 stage 的 ModelWorkerPool。

    职责：
    - 管理 StageAwareDispatcher（取帧分组）
    - 为每个 stage 创建 MultiModelWorkerPool
    - 启动推理线程，消费各 stage 的批量请求
    - 将 FrameDetections 同步到 ClientQueues.slide_window
    """

    def __init__(
        self,
        client_queues_map: Optional[Dict[str, ClientQueues]] = None,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        max_batch_per_stage: int = 8,
        use_cuda_stream: bool = True,
        client_manager_instance: Optional[ClientManager] = None,
        feature_store: Optional[Any] = None,
    ):
        """
        Args:
            client_queues_map: {client_id: ClientQueues}，如果为 None 则从 client_manager 获取
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
            use_cuda_stream: 是否使用 CUDA Stream 并行
            client_manager_instance: ClientManager 实例（可选，用于动态获取客户端）
        """

        # 保存 ClientManager 实例（用于动态客户端管理）
        self._client_manager = client_manager_instance or client_manager

        # L2 特征落盘（常开；离线链路硬需求）。由 InferenceManager 注入并管理生命周期
        self._feature_store = feature_store

        # 客户端队列映射（可能动态更新）
        if client_queues_map is None:
            # 从 ClientManager 获取
            self.client_queues_map = self._client_manager.get_all_clients()
        else:
            self.client_queues_map = client_queues_map

        # Stage 配置（必须提供）
        if stage_configs is None:
            raise ValueError("stage_configs 不能为 None，请提供 Stage 配置")
        self.stage_configs = stage_configs

        self.max_batch_per_stage = max_batch_per_stage
        self.use_cuda_stream = use_cuda_stream

        # 创建 Dispatcher（直接引用 ClientManager）
        self.dispatcher = StageAwareDispatcher(
            max_batch_per_stage=max_batch_per_stage,
            client_manager_instance=self._client_manager,
        )

        # 为每个 stage 创建 MultiModelWorkerPool
        self.worker_pools: Dict[str, MultiModelWorkerPool] = {}
        for stage, cfg in self.stage_configs.items():
            # 使用 InferenceWorkflow 列表
            models = cfg.get("models", [])
            if models:
                self.worker_pools[stage] = MultiModelWorkerPool(
                    stage=stage,
                    models=models,
                    use_cuda_stream=use_cuda_stream,
                )

        self._stop_event = threading.Event()
        self._worker_threads: List[threading.Thread] = []

        # 取实际生效的 CUDA stream 状态（由 WorkerPool 根据硬件判断）
        actual_cuda = any(
            pool.use_cuda_stream for pool in self.worker_pools.values()
        )
        logger.info(
            f"ModelWorkerService initialized: stages={list(self.worker_pools.keys())}, "
            f"CUDA_stream={'enabled' if actual_cuda else 'disabled'}, "
            f"clients={len(self.client_queues_map)}"
        )

    def start(self):
        """启动服务：Dispatcher + 推理线程 + 模型预热"""
        self.dispatcher.start()

        # 为每个 stage 启动一个推理线程
        for stage in self.worker_pools.keys():
            from functools import partial
            thread = threading.Thread(
                target=guarded_run,
                args=(partial(self._inference_loop, stage), self._stop_event, f"InferWorker-{stage}"),
                daemon=True,
                name=f"InferWorker-{stage}",
            )
            thread.start()
            self._worker_threads.append(thread)

        logger.info(f"Started {len(self._worker_threads)} inference worker threads")

        # ========== 模型预热 ==========
        logger.info("Starting model warmup for all stages...")
        warmup_start = time.time()

        for stage, worker_pool in self.worker_pools.items():
            # 获取该stage的批大小
            batch_size = self.stage_configs[stage].get(
                "batch_size", self.max_batch_per_stage
            )

            # 执行预热
            try:
                worker_pool.warmup(batch_size=batch_size)
            except Exception as e:
                logger.error(
                    f"Model warmup failed for stage {stage}: {e}", exc_info=True
                )

        warmup_elapsed = time.time() - warmup_start
        logger.info(
            f"Model warmup completed for all stages: "
            f"elapsed={warmup_elapsed*1000:.1f}ms"
        )

    def stop(self):
        """停止服务"""
        self._stop_event.set()
        self.dispatcher.stop()

        for thread in self._worker_threads:
            thread.join(timeout=2.0)
            # infer_batch(CUDA 同步)是唯一不可中断窗口：wedge 时 join 超时、线程被 daemon 强杀。
            # 无法根治，仅在此留诊断痕迹（关键 flush 已在 InferenceManager.stop 控制线程完成，硬杀不丢数据）。
            if thread.is_alive():
                logger.warning(
                    "[ModelWorkerService] %s 未在 2s 内退出（疑似卡在 infer_batch/CUDA），将被 daemon 强杀",
                    thread.name,
                )

        logger.info("ModelWorkerService stopped")

    def _inference_loop(self, stage: str):
        """推理循环：消费指定 stage 的批量请求（支持自适应超时）。"""
        worker_pool = self.worker_pools[stage]
        batch_size = self.stage_configs[stage].get(
            "batch_size", self.max_batch_per_stage
        )

        while not self._stop_event.is_set():
            try:
                # 获取队列深度（用于自适应超时）
                queue_depth = self.dispatcher.queue_depth(stage)

                # 自适应超时：针对小并发优化（<10客户端），避免过度等待增加延迟
                if queue_depth >= batch_size * 2:
                    timeout_ms = 1.0  # 队列充足，立即触发
                elif queue_depth >= batch_size:
                    timeout_ms = 2.0  # 队列适中，短暂等待
                else:
                    timeout_ms = 3.0  # 队列不足，稍微等待（避免过度等待）

                # 从 Dispatcher 获取该 stage 的 batch（带超时）
                batch = self.dispatcher.get_batch_for_stage(
                    stage, max_size=batch_size, timeout_ms=timeout_ms
                )

                if not batch:
                    # 没有请求，短暂休眠
                    time.sleep(0.01)
                    continue

                # 批量推理
                start_time = time.time()
                results = worker_pool.infer_batch(batch)
                elapsed = time.time() - start_time

                # 回写结果到 ClientQueues
                self._write_back_results(results)

                # 调试日志（增加队列深度信息）
                if len(batch) > 0 and logger.isEnabledFor(logging.DEBUG):
                    fps = len(batch) / elapsed if elapsed > 0 else 0
                    logger.debug(
                        "[Worker-%s] Batch processed: size=%d/%d, time=%.1fms, fps=%.1f, queue_depth=%d, timeout=%.1fms",
                        stage, len(batch), batch_size, elapsed*1000, fps, queue_depth, timeout_ms
                    )

            except FrameDrop as e:
                # 边界层 1: FrameDrop - 静默丢弃（warning级别）
                logger.warning(
                    "[BoundaryLayer1][Worker-%s] Frame dropped: client=%s, reason=%s",
                    stage, e.client_id, e.reason
                )
                continue  # 继续处理下一批

            except ModelInferenceError as e:
                # 边界层 1: 模型推理错误 - ERROR级别，记录上下文
                logger.error(
                    "[BoundaryLayer1][Worker-%s] Model inference failed: %s", stage, e,
                    exc_info=True,
                    extra={
                        "client_id": e.client_id,
                        "model_name": e.model_name,
                        "is_cuda_error": e.is_cuda_error,
                    },
                )
                # 记录GPU OOM指标
                if e.is_cuda_error:
                    gpu_oom_total.labels(
                        model=getattr(e, "model_name", "unknown")
                    ).inc()
                # 继续处理下一批
                time.sleep(0.1)
                continue

            except AppError as e:
                # 边界层 1: 其他应用异常 - ERROR级别
                logger.error(
                    "[BoundaryLayer1][Worker-%s] Application error: %s", stage, e,
                    exc_info=True,
                    extra={"client_id": getattr(e, "client_id", None)},
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
        """将推理结果双写到 ClientQueues。

        双写策略：
        - slide_window（per-task 拆分）：供 TemporalWorker 历史窗口分析
        - latest_inference（原子快照）：供 VisualizationWorker 直接读取，保证同帧一致性

        异常处理：
        - 单条结果对应的客户端已移除 → 跳过该条（per-result），不中断整批。
          一个 batch 跨多客户端填充，某客户端断连不得殃及同批其他在线客户端的写回。
        """
        for res in results:
            if not self._client_manager.has_client(res.client_id):
                # 该客户端已移除：仅跳过此条，继续处理同批其他客户端
                logger.debug(
                    "[Worker] Skip write-back for removed client=%s", res.client_id
                )
                continue

            cq = self._client_manager.get_client(res.client_id)
            # Path 1: per-task slide_window（temporal 需要历史窗口）
            for task_name, detection_output in res.detections.items():
                cq.push_detection(task_name, detection_output)
            # Path 2: 原子快照（visualization 只需最新，保证所有 task 同帧一致）
            cq.set_latest_inference(res)
            # L2 特征落盘（常开）：offline 链路硬需求，best-effort 不影响主链路。
            # 目录键 (task_id, step_id) 与 HLS 同款；任一为 None 则跳过（拒落，同 HLS 口径）。
            # task 取一次再派生 task_id/step_id，避免两次独立读之间 set_task 造成键错配。
            if self._feature_store is not None:
                task = cq.get_task()
                task_id = task.task_id if task else None
                step_id = cq.step_id_of(task)
                if task_id is not None and step_id is not None:
                    self._feature_store.append(task_id, step_id, res)

