"""模型推理服务：统一管理多个 stage 的 ModelWorkerPool。

职责：
- 管理 StageAwareDispatcher（取帧分组）
- 为每个 stage 创建 MultiModelWorkerPool
- 启动推理线程，消费各 stage 的批量请求
- 将结果回写到 ClientQueues
- 更新 ClientState（业务状态管理）
"""

from __future__ import annotations

import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, Dict, List, Optional

import numpy as np

from app.models.frame import FrameData
from app.services.inference.dispatcher import StageAwareDispatcher
from app.services.inference.models import InferenceResult
from app.services.inference.worker_pool import MultiModelWorkerPool

if TYPE_CHECKING:
    from app.services.client import ClientQueues, ClientState
    from app.services.client_manager import ClientManager

# 避免循环导入，延迟导入
from app.services.client_manager import client_manager


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
        client_queues_map: Optional[Dict[str, ClientQueues]] = None,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        max_batch_per_stage: int = 8,
        use_cuda_stream: bool = True,
        num_worker_threads: int = 2,  # 每个 stage 一个推理线程
        client_manager_instance: Optional[ClientManager] = None,
    ):
        """
        Args:
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

        # 创建 Dispatcher
        self.dispatcher = StageAwareDispatcher(
            client_queues_map=self.client_queues_map,
            max_batch_per_stage=max_batch_per_stage,
        )

        # 为每个 stage 创建 MultiModelWorkerPool
        self.worker_pools: Dict[str, MultiModelWorkerPool] = {}
        for stage, cfg in self.stage_configs.items():
            # 支持新旧两种配置格式：
            # - 新格式：models (InferenceTask 列表)
            # - 旧格式：subtasks (SubtaskPipeline 列表，向后兼容)
            models = cfg.get("models", cfg.get("subtasks", []))
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

        print(
            f"[ModelWorkerService] 初始化完成: stages={list(self.worker_pools.keys())}, "
            f"CUDA Stream={'enabled' if use_cuda_stream else 'disabled'}, "
            f"clients={len(self.client_queues_map)}"
        )

    def start(self):
        """启动服务：Dispatcher + 推理线程"""
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

        print(f"[ModelWorkerService] 已启动 {len(self._worker_threads)} 个推理线程")

    def stop(self):
        """停止服务"""
        self._stop_event.set()
        self.dispatcher.stop()
        self.executor.shutdown(wait=True)

        for thread in self._worker_threads:
            thread.join(timeout=2.0)

        print("[ModelWorkerService] 已停止")

    def _inference_loop(self, stage: str):
        """推理循环：消费指定 stage 的批量请求。"""
        worker_pool = self.worker_pools[stage]
        batch_size = self.stage_configs[stage].get(
            "batch_size", self.max_batch_per_stage
        )

        while not self._stop_event.is_set():
            try:
                # 从 Dispatcher 获取该 stage 的 batch
                batch = self.dispatcher.get_batch_for_stage(stage, max_size=batch_size)

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

                # 调试日志
                if len(batch) > 0:
                    fps = len(batch) / elapsed if elapsed > 0 else 0
                    print(
                        f"[InferWorker-{stage}] 完成 batch_size={len(batch)}, "
                        f"耗时={elapsed*1000:.1f}ms, 吞吐={fps:.1f}fps"
                    )

            except Exception as e:
                print(f"[InferWorker-{stage}] 异常: {e}")
                import traceback

                traceback.print_exc()
                time.sleep(0.1)

    def _write_back_results(self, results: List[InferenceResult]):
        """将推理结果回写到 ClientQueues，并更新 ClientState。

        容错设计：
        - 推理完成时客户端可能已被清理（连接断开、任务完成等）
        - 使用 get() 安全检查，客户端不存在时跳过回写
        - 不会因为客户端清理而导致推理服务崩溃
        """
        dropped_count = 0
        for res in results:
            # 安全检查：客户端可能在推理过程中被清理
            cq = self.client_queues_map.get(res.client_id)
            if cq is None:
                # 客户端已被清理，丢弃此结果
                dropped_count += 1
                continue

            # 更新 ClientState（业务状态）
            if hasattr(cq, "state") and cq.state is not None:
                self._update_client_state(cq.state, res)

            # TODO: 实现可视化逻辑
            # 需要调用对应 task 的 visualize 方法，在帧上绘制检测结果
            # 参考：app/services/task_pipeline/leak/leak_test.py 中的可视化实现
            annotated_frame = self._visualize_result(res, cq)

            # 如果有可视化后的帧，则写入队列
            if annotated_frame is not None:
                frame_data = FrameData(
                    timestamp=res.timestamp,
                    frame=annotated_frame,
                    inference_result=res.result,
                )

                # 写入队列
                cq.append_ca_processed(frame_data)
                cq.append_rt_processed(frame_data)

        # 日志：如果有结果被丢弃，记录一下
        if dropped_count > 0:
            print(f"[InferWorker] 丢弃了 {dropped_count} 个无效结果（客户端已删除）")

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
        """刷新客户端队列映射（从 ClientManager 获取最新）。

        适用场景：
        - 新客户端通过 RTSP/WebSocket 连接加入
        - 客户端任务完成或断开连接被清理
        - 定期同步（推荐每 5-10 秒调用一次）

        注意：
        - 客户端是动态创建和清理的，初始化时获取的是快照
        - 新客户端加入后，必须调用此方法才能被推理服务识别
        - 客户端离开后，推理服务会自动跳过（安全检查）

        使用示例：
        ```python
        # 方式 1: 定期刷新（推荐）
        import threading
        import time

        def refresh_loop():
            while True:
                service.refresh_client_queues()
                time.sleep(5)

        threading.Thread(target=refresh_loop, daemon=True).start()

        # 方式 2: 事件驱动刷新
        def on_client_added(client_id):
            service.refresh_client_queues()

        def on_client_removed(client_id):
            service.refresh_client_queues()
        ```
        """
        new_map = self._client_manager.get_all_clients()

        # 统计变化
        old_count = len(self.client_queues_map)
        new_count = len(new_map)

        self.client_queues_map = new_map
        self.dispatcher.client_queues_map = new_map

        if new_count != old_count:
            print(
                f"[ModelWorkerService] 客户端列表已更新: "
                f"{old_count} → {new_count} ({new_count - old_count:+d})"
            )
        else:
            print(f"[ModelWorkerService] 客户端列表已刷新: {new_count} 个客户端")
