"""visualization.py - 可视化工作线程池。

职责：
- 从可视化队列消费时序分析后的数据包
- 取当前客户端的最新帧（原始帧流）
- 绘制检测框、标注、文字信息到最新帧上
- 若没有新的检测结果，继续沿用上一次的检测结果
- 投递到写回队列
"""

import logging
import threading
from queue import Empty, Queue
from typing import Any, Dict, List, Optional, Tuple

import numpy as np

from app.services.client import client_manager
from app.services.inference.models import (
    TemporalAnalysisPackage,
    TemporalAnalysisResult,
    WriteBackData,
)

logger = logging.getLogger(__name__)


class Visualizer:
    """可视化器抽象接口。

    子类需要实现 visualize 方法，用于绘制检测框和标注。
    """

    def visualize(
        self,
        frame: np.ndarray,
        inference_result: Dict[str, Any],
        stage: str,
        temporal_result: Optional[TemporalAnalysisResult] = None,
    ) -> np.ndarray:
        """可视化推理结果。

        Args:
            frame: 原始帧
            inference_result: 推理结果
            stage: 当前阶段
            temporal_result: 时序分析结果（可选）

        Returns:
            可视化后的帧
        """
        # 默认实现：返回原始帧（无可视化）
        return frame.copy()


class VisualizationWorker:
    """可视化工作线程。"""

    def __init__(
        self,
        input_queue: Queue,  # 输入：时序分析后的数据包
        output_queue: Queue,  # 输出：完整数据包（含可视化帧）
        visualizer: Visualizer,
        stop_event: threading.Event,
        worker_id: int = 0,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,  # 新增：Stage 配置
        use_fixed_visualizer: bool = False,  # 新增：是否使用固定渲染器
    ):
        """初始化可视化工作线程。

        Args:
            input_queue: 时序分析数据包队列
            output_queue: 写回数据包队列
            visualizer: 可视化器（向后兼容）
            stop_event: 停止事件
            worker_id: 工作线程ID（用于调试）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
            use_fixed_visualizer: 是否使用新的固定可视化渲染器
        """
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.visualizer = visualizer  # 保留用于向后兼容
        self.stop_event = stop_event
        self.worker_id = worker_id
        self.stage_configs = stage_configs or {}
        self.use_fixed_visualizer = use_fixed_visualizer

        # 固定可视化渲染器（新架构）
        if use_fixed_visualizer:
            from app.services.inference.components import FixedVisualizer
            self.fixed_visualizer = FixedVisualizer()
        else:
            self.fixed_visualizer = None

        # 缓存每个客户端的最新检测结果（用于降帧补偿）
        self.latest_results: Dict[
            str, Tuple[Dict[str, Any], Optional[TemporalAnalysisResult]]
        ] = {}

    def run(self):
        """工作循环。"""
        logger.debug("[VisualizationWorker-%d] Started", self.worker_id)

        while not self.stop_event.is_set():
            try:
                # 1. 从队列获取数据包（包含推理结果）
                try:
                    package: TemporalAnalysisPackage = self.input_queue.get(timeout=0.1)
                except Empty:
                    continue

                # 2. 更新该客户端的最新检测结果
                self.latest_results[package.client_id] = (
                    package.inference_result,
                    package.temporal_result,
                )

                # 3. 获取客户端的最新原始帧
                if not client_manager.has_client(package.client_id):
                    continue

                cq = client_manager.get_client(package.client_id)
                if cq is None:
                    continue

                latest_frame = cq.get_latest_frame()  # 取当前最新帧
                if latest_frame is None:
                    # 如果没有更新的帧，使用推理时的帧
                    latest_frame = package.raw_frame

                # 4. 可视化：使用新架构或回退到旧逻辑
                if self.use_fixed_visualizer and self.fixed_visualizer:
                    annotated_frame = self._visualize_with_fixed_renderer(
                        latest_frame, package
                    )
                else:
                    # 回退到旧的可视化器
                    annotated_frame = self.visualizer.visualize(
                        frame=latest_frame,
                        inference_result=package.inference_result,
                        stage=package.stage,
                        temporal_result=package.temporal_result,
                    )

                # 5. 组装完整数据包
                write_back_data = WriteBackData(
                    client_id=package.client_id,
                    timestamp=package.timestamp,  # 保持推理时间戳
                    stage=package.stage,
                    processed_frame=annotated_frame,
                    inference_result=package.inference_result,
                    frontend_message=package.frontend_message,
                    temporal_result=package.temporal_result,
                )

                # 6. 投递到写回队列
                self.output_queue.put(write_back_data)

            except Exception as e:
                logger.error(f"[VisualizationWorker-{self.worker_id}] 异常: {e}", exc_info=True)

        logger.debug(f"[VisualizationWorker-{self.worker_id}] 已停止")

    def _visualize_with_fixed_renderer(
        self, frame: np.ndarray, package: TemporalAnalysisPackage
    ) -> np.ndarray:
        """使用新的固定渲染器进行可视化
        
        Args:
            frame: 原始帧
            package: 时序分析数据包
            
        Returns:
            可视化后的帧
        """
        try:
            # 获取当前 stage 的 tasks
            stage_cfg = self.stage_configs.get(package.stage, {})
            tasks = stage_cfg.get("models", [])
            
            if not tasks:
                # 无 tasks，回退到旧逻辑
                return self.visualizer.visualize(
                    frame=frame,
                    inference_result=package.inference_result,
                    stage=package.stage,
                    temporal_result=package.temporal_result,
                )
            
            # 导入 VisualizationData（避免循环导入）
            from app.services.inference.data_models import VisualizationData
            
            vis_data_list: List[VisualizationData] = []
            temporal_events: List[str] = []
            
            # 处理每个 task
            for task in tasks:
                task_result = package.inference_result.get(task.name, {})
                
                # 检查是否有新的 detection_output
                if "detection_output" not in task_result:
                    continue  # 跳过没有使用新架构的 task
                
                detection_output = task_result["detection_output"]
                
                # 获取对应的时序结果（需要重新计算，或从 package 中提取）
                # 简化处理：使用空的 TemporalResult
                from app.services.inference.data_models import TemporalResult
                
                # 这里应该调用 Task 的 analyze_temporal()，但由于已经在 TemporalWorker 中处理过了
                # 我们可以从 package.temporal_result.events 中提取信息
                # 为了简化，这里创建一个简单的 TemporalResult
                temporal_res = TemporalResult(
                    detected=len(detection_output.detections) > 0,
                    event_triggered=package.temporal_result.step_completed if package.temporal_result else False,
                    event_message=None,
                    counters={},
                )
                
                # 调用 Task 的 prepare_visualization_data()
                vis_data = task.prepare_visualization_data(detection_output, temporal_res)
                vis_data_list.append(vis_data)
            
            # 提取时序事件
            if package.temporal_result and package.temporal_result.events:
                temporal_events = package.temporal_result.events
            
            # 使用固定渲染器渲染
            annotated_frame = self.fixed_visualizer.render(
                frame=frame.copy(),
                vis_data_list=vis_data_list,
                stage=package.stage,
                temporal_events=temporal_events,
            )
            
            return annotated_frame
            
        except Exception as e:
            logger.error(f"[VisualizationWorker] Fixed renderer failed: {e}, fallback to old visualizer", exc_info=True)
            # 回退到旧逻辑
            return self.visualizer.visualize(
                frame=frame,
                inference_result=package.inference_result,
                stage=package.stage,
                temporal_result=package.temporal_result,
            )

    def visualize_with_cached_result(
        self, client_id: str, current_frame: np.ndarray
    ) -> Optional[np.ndarray]:
        """使用缓存的检测结果可视化当前帧（用于未推理的中间帧）。

        Args:
            client_id: 客户端ID
            current_frame: 当前最新帧

        Returns:
            可视化后的帧，若无缓存结果则返回None
        """
        if client_id not in self.latest_results:
            return None

        inference_result, temporal_result = self.latest_results[client_id]

        # 使用缓存的检测结果绘制当前帧
        return self.visualizer.visualize(
            frame=current_frame,
            inference_result=inference_result,
            stage=temporal_result.stage if temporal_result else "UNKNOWN",
            temporal_result=temporal_result,
        )


class VisualizationWorkerPool:
    """可视化线程池。"""

    def __init__(
        self,
        input_queue: Queue,
        output_queue: Queue,
        visualizer: Visualizer,
        num_workers: int = 4,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,  # 新增：Stage 配置
        use_fixed_visualizer: bool = False,  # 新增：是否使用固定渲染器
    ):
        """初始化可视化线程池。

        Args:
            input_queue: 时序分析数据包队列
            output_queue: 写回数据包队列
            visualizer: 可视化器（向后兼容）
            num_workers: 工作线程数量
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
            use_fixed_visualizer: 是否使用新的固定可视化渲染器
        """
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.visualizer = visualizer
        self.num_workers = num_workers
        self.stage_configs = stage_configs or {}
        self.use_fixed_visualizer = use_fixed_visualizer

        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []

    def start(self):
        """启动线程池。"""
        for i in range(self.num_workers):
            worker = VisualizationWorker(
                input_queue=self.input_queue,
                output_queue=self.output_queue,
                visualizer=self.visualizer,
                stop_event=self._stop_event,
                worker_id=i,
                stage_configs=self.stage_configs,  # 传递 stage_configs
                use_fixed_visualizer=self.use_fixed_visualizer,  # 传递 use_fixed_visualizer
            )

            thread = threading.Thread(
                target=worker.run,
                daemon=True,
                name=f"VisualizationWorker-{i}",
            )
            thread.start()
            self._workers.append(thread)

        logger.info("[VisualizationWorkerPool] Started %d workers", self.num_workers)

    def stop(self):
        """停止线程池。"""
        self._stop_event.set()

        for thread in self._workers:
            thread.join(timeout=2.0)

        logger.debug("[VisualizationWorkerPool] Stopped")
