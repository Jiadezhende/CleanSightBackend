"""模型推理服务：统一管理多个 stage 的 ModelWorkerPool。

职责：
- 管理 StageAwareDispatcher（取帧分组）
- 为每个 stage 创建 MultiModelWorkerPool
- 启动推理线程，消费各 stage 的批量请求
- 将结果回写到 ClientQueues
- 更新 ClientState（业务状态管理）
"""

from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import numpy as np

from app.models.frame import FrameData
from app.services.inference.core.dispatcher import StageAwareDispatcher
from app.services.inference.models import InferenceResult
from app.services.inference.workers.base import MultiModelWorkerPool
from app.utils.exceptions import (
    AppError,
    FrameDrop,
    ModelInferenceError,
    PersistenceError,
)
from app.utils.metrics import gpu_oom_total

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.client import ClientManager, ClientQueues, ClientState

# 避免循环导入，延迟导入
from app.services.client import client_manager


class ModelWorkerService:
    """模型推理服务：统一管理多个 stage 的 ModelWorkerPool。

    职责：
    - 管理 StageAwareDispatcher（取帧分组）
    - 为每个 stage 创建 MultiModelWorkerPool
    - 启动推理线程，消费各 stage 的批量请求
    - 将结果回写到 ClientQueues
    - 更新 ClientState（业务状态管理）
    """

    def __init__(
        self,
        temporal_queue,  # 时序队列（必需参数）
        client_queues_map: Optional[Dict[str, ClientQueues]] = None,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        max_batch_per_stage: int = 8,
        use_cuda_stream: bool = True,
        num_worker_threads: int = 2,  # 每个 stage 一个推理线程
        client_manager_instance: Optional[ClientManager] = None,
    ):
        """
        Args:
            temporal_queue: 时序队列（queue.Queue），用于投递推理结果（异步架构）
            client_queues_map: {client_id: ClientQueues}，如果为 None 则从 client_manager 获取
            stage_configs: Stage 配置（完全解耦版本，使用 InferenceTask）
                {
                    "LEAK": {
                        "models": [bubble_task, bending_task],  # InferenceTask 实例列表
                        "batch_size": 4,
                    },
                    "CLEAN": {
                        "models": [quality_task],  # InferenceTask 实例列表
                        "batch_size": 6,
                    },
                }
            max_batch_per_stage: 每个 stage 最大 batch 大小
            use_cuda_stream: 是否使用 CUDA Stream 并行
            num_worker_threads: 推理线程数
            client_manager_instance: ClientManager 实例（可选，用于动态获取客户端）
        """
        # 保存时序队列（异步架构）
        self.temporal_queue = temporal_queue

        # 保存 ClientManager 实例（用于动态客户端管理）
        self._client_manager = client_manager_instance or client_manager

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
            # 使用 InferenceTask 列表
            models = cfg.get("models", [])
            if models:
                self.worker_pools[stage] = MultiModelWorkerPool(
                    stage=stage,
                    models=models,
                    use_cuda_stream=use_cuda_stream,
                )

        # 推理线程池
        self.executor = ThreadPoolExecutor(
            max_workers=num_worker_threads, thread_name_prefix="InferWorker"
        )
        self._stop_event = threading.Event()
        self._worker_threads: List[threading.Thread] = []

        logger.info(
            f"ModelWorkerService initialized: stages={list(self.worker_pools.keys())}, "
            f"CUDA_stream={'enabled' if use_cuda_stream else 'disabled'}, "
            f"clients={len(self.client_queues_map)}"
        )

    def start(self):
        """启动服务：Dispatcher + 推理线程 + 模型预热"""
        self.dispatcher.start()

        # 为每个 stage 启动一个推理线程
        for stage in self.worker_pools.keys():
            thread = threading.Thread(
                target=self._inference_loop,
                args=(stage,),
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
        self.executor.shutdown(wait=True)

        for thread in self._worker_threads:
            thread.join(timeout=2.0)

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
                with self.dispatcher._lock:
                    queue_depth = len(self.dispatcher._stage_queues.get(stage, []))

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
                if len(batch) > 0:
                    fps = len(batch) / elapsed if elapsed > 0 else 0
                    logger.info(
                        f"[Worker-{stage}] Batch processed: "
                        f"size={len(batch)}/{batch_size}, "
                        f"time={elapsed*1000:.1f}ms, fps={fps:.1f}, "
                        f"queue_depth={queue_depth}, timeout={timeout_ms:.1f}ms"
                    )

            except FrameDrop as e:
                # 边界层 1: FrameDrop - 静默丢弃（DEBUG级别，无traceback）
                logger.debug(
                    f"[BoundaryLayer1][Worker-{stage}] Frame dropped: "
                    f"client={e.client_id}, reason={e.reason}"
                )
                continue  # 继续处理下一批

            except ModelInferenceError as e:
                # 边界层 1: 模型推理错误 - ERROR级别，记录上下文
                logger.error(
                    f"[BoundaryLayer1][Worker-{stage}] Model inference failed: {e}",
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
                    f"[BoundaryLayer1][Worker-{stage}] Application error: {e}",
                    exc_info=True,
                    extra={"client_id": getattr(e, "client_id", None)},
                )
                time.sleep(0.1)
                continue

            except Exception as e:
                # 边界层 1: 未预期的异常 - CRITICAL级别
                logger.critical(
                    f"[BoundaryLayer1][Worker-{stage}] Unexpected error: {e}",
                    exc_info=True,
                )
                time.sleep(0.5)
                continue

    def _write_back_results(self, results: List[InferenceResult]):
        """将推理结果投递到时序队列（异步架构）

        异常处理：
        - 客户端已移除 → 抛出 FrameDrop
        - 队列已满 → 抛出 PersistenceError（retryable=True）
        """
        from app.utils.exceptions import FrameDrop, PersistenceError

        for res in results:
            # 检查客户端是否存在（可能在推理过程中被移除）
            if not self._client_manager.has_client(res.client_id):
                # 构建 FrameDrop 参数（frame_index 可能不存在）
                frame_idx = getattr(res, "frame_index", None)
                raise FrameDrop(
                    client_id=res.client_id,
                    frame_index=frame_idx,  # type: ignore
                    reason="client_removed",
                )

            # 投递到时序队列
            try:
                self.temporal_queue.put(res, timeout=0.1)
            except Exception:  # queue.Full or asyncio.TimeoutError
                # 实时推理场景：队列满时丢弃当前帧，继续处理下一帧
                raise FrameDrop(
                    client_id=res.client_id,
                    frame_index=getattr(res, "frame_index", None),
                    reason="queue_timeout",
                )

    def _update_client_state(self, state: ClientState, result: InferenceResult):
        """更新客户端业务状态（可由子类覆写）。

        Args:
            state: 客户端状态
            result: 推理结果

        示例逻辑：
        - 检测到气泡 → 递增连续气泡计数
        - 连续气泡达到阈值 → 标记步骤完成
        """
        # 示例：处理 LEAK stage 的气泡检测
        if result.stage == "LEAK":
            bubble_res = result.result.get("bubble", {})
            if isinstance(bubble_res, dict) and bubble_res.get("bubble_detected"):
                # 递增连续气泡计数
                count = state.increment_counter("continuous_bubble")
                # 达到阈值则标记完成
                if count >= 3:  # 阈值可配置
                    state.mark_step_completed()
            else:
                # 未检测到气泡，重置计数
                state.reset_counter("continuous_bubble")

    def _visualize_result(
        self,
        result: InferenceResult,
        cq: ClientQueues,
    ) -> Optional[np.ndarray]:
        """可视化推理结果（可由子类或外部覆写）。

        Args:
            result: 推理结果
            cq: 客户端队列

        Returns:
            可视化后的帧（如果有），否则返回 None

        TODO: 实现完整的可视化流程
        需要：
        1. 从 cq.get_latest_raw_frame() 获取原始帧
        2. 调用各 subtask 的 task.visualize() 方法绘制检测框/标注
        3. 添加文字信息（stage、timestamp、fps 等）
        4. 返回可视化后的帧
        参考：LeakBubblePipelineService._annotate_frame() 的实现
        """
        # 默认实现：返回 None（无可视化）
        # 子类可以覆写这个方法，或者在外部注入可视化函数
        return None

    def refresh_client_queues(self):
        """刷新客户端队列映射（已优化为实时同步，保留向后兼容）。

        ✅ **架构改进**：
        - Dispatcher 现在直接引用 ClientManager，客户端变化自动同步
        - 新客户端加入立即生效，无需手动刷新
        - 此方法保留仅为向后兼容和日志统计

        历史行为：
        - 旧版本需要定期调用此方法刷新客户端列表（5秒延迟）
        - 新版本无需手动调用，客户端变化实时生效

        迁移指南：
        - 现有调用此方法的代码可以安全保留（无副作用）
        - 新代码无需调用此方法
        - 定期刷新线程可以移除（可选）
        """
        # 更新本地快照（仅用于日志统计）
        new_map = self._client_manager.get_all_clients()
        old_count = len(self.client_queues_map)
        new_count = len(new_map)

        self.client_queues_map = new_map
        # 注意：Dispatcher 不再需要更新（已直接引用 ClientManager）

        if new_count != old_count:
            logger.info(
                f"[RealTimeSync] Client count changed: "
                f"{old_count} → {new_count} ({new_count - old_count:+d})"
            )
        else:
            logger.debug(f"[RealTimeSync] Client count unchanged: {new_count}")
