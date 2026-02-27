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
from datetime import datetime
from queue import Empty, Queue
from typing import Any, Dict, List, Optional, Tuple

import cv2
import numpy as np

from app.services.inference.data_models import (
    DetectionOutput,
    TemporalResult,
    VisualizationData,
    VisItem,
    VisualizationType,
)

from app.services.client import client_manager
from app.services.inference.models import (
    TemporalAnalysisPackage,
    TemporalAnalysisResult,
    WriteBackData,
)

logger = logging.getLogger(__name__)



class VisualizationWorker:
    """可视化工作线程。"""

    def __init__(
        self,
        input_queue: Queue,  # 输入：时序分析后的数据包
        output_queue: Queue,  # 输出：完整数据包（含可视化帧）
        stop_event: threading.Event,
        worker_id: int = 0,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化可视化工作线程。

        Args:
            input_queue: 时序分析数据包队列
            output_queue: 写回数据包队列
            stop_event: 停止事件
            worker_id: 工作线程ID（用于调试）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.stop_event = stop_event
        self.worker_id = worker_id
        self.stage_configs = stage_configs or {}
        self.fixed_visualizer = FixedVisualizer()

        # 缓存每个客户端的最新检测结果（用于降帧补偿）
        self.latest_results: Dict[
            str, Tuple[str, Dict[str, Any], Optional[TemporalAnalysisResult]]
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
                    package.stage,
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

                # 4. 可视化
                annotated_frame = self._visualize_with_fixed_renderer(
                    latest_frame, package
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
                return frame.copy()
            
            vis_data_list: List[VisualizationData] = []
            temporal_events: List[str] = []

            for task in tasks:
                detection_output = package.inference_result.get(task.name)

                if not isinstance(detection_output, DetectionOutput):
                    continue

                temporal_res = TemporalResult(
                    detected=len(detection_output.detections) > 0,
                    event_triggered=package.temporal_result.step_completed if package.temporal_result else False,
                    event_message=None,
                    counters={},
                )

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
            logger.error(f"[VisualizationWorker] Fixed renderer failed: {e}", exc_info=True)
            return frame.copy()

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

        stage, inference_result, temporal_result = self.latest_results[client_id]

        # 用缓存结果创建一个临时 package 并渲染
        from app.services.inference.models import TemporalAnalysisPackage
        import numpy as np
        mock_pkg = TemporalAnalysisPackage(
            client_id=client_id,
            timestamp=0.0,
            stage=stage,
            inference_result=inference_result,
            temporal_result=temporal_result,
            frontend_message=None,
            raw_frame=current_frame,
        )
        return self._visualize_with_fixed_renderer(current_frame, mock_pkg)


class VisualizationWorkerPool:
    """可视化线程池。"""

    def __init__(
        self,
        input_queue: Queue,
        output_queue: Queue,
        num_workers: int = 4,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化可视化线程池。

        Args:
            input_queue: 时序分析数据包队列
            output_queue: 写回数据包队列
            num_workers: 工作线程数量
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.num_workers = num_workers
        self.stage_configs = stage_configs or {}

        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []

    def start(self):
        """启动线程池。"""
        for i in range(self.num_workers):
            worker = VisualizationWorker(
                input_queue=self.input_queue,
                output_queue=self.output_queue,
                stop_event=self._stop_event,
                worker_id=i,
                stage_configs=self.stage_configs,
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


class FixedVisualizer:
    """固定可视化渲染器

    根据 Task 提供的 VisualizationData 渲染视频帧，无需针对每个任务编写可视化代码。
    支持多种可视化类型：BBox、Segmentation、Keypoint
    """

    def render(
        self,
        frame: np.ndarray,
        vis_data_list: List["VisualizationData"],
        stage: str,
        temporal_events: Optional[List[str]] = None,
    ) -> np.ndarray:
        """渲染所有Task的可视化数据

        Args:
            frame: 原始帧
            vis_data_list: Task提供的可视化数据列表
            stage: 当前阶段（如"LEAK"）
            temporal_events: 时序事件列表（如["连续3帧检测到气泡"]）

        Returns:
            可视化后的帧
        """
        if frame is None:
            return frame

        annotated = frame.copy()

        for vis_data in vis_data_list:
            if vis_data.type == VisualizationType.BBOX:
                self._draw_bboxes(annotated, vis_data.items)
            elif vis_data.type == VisualizationType.MASK:
                self._draw_masks(annotated, vis_data.items)
            elif vis_data.type == VisualizationType.KEYPOINT:
                self._draw_keypoints(annotated, vis_data.items)

            self._draw_status_bar(
                annotated,
                vis_data.status_text,
                vis_data.status_color,
                vis_data.status_position,
            )

        self._draw_global_info(annotated, stage, temporal_events)
        return annotated

    def _draw_bboxes(self, frame: np.ndarray, items: List["VisItem"]):
        for item in items:
            if item.bbox is None:
                continue
            x1, y1, x2, y2 = item.bbox
            cv2.rectangle(frame, (x1, y1), (x2, y2), item.color, 2)
            if item.label:
                (label_w, label_h), _ = cv2.getTextSize(
                    item.label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
                )
                label_y = max(y1 - 10, label_h + 5)
                cv2.rectangle(
                    frame,
                    (x1, label_y - label_h - 5),
                    (x1 + label_w + 6, label_y + 2),
                    item.color,
                    -1,
                )
                cv2.putText(
                    frame, item.label, (x1 + 3, label_y - 3),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA,
                )

    def _draw_masks(self, frame: np.ndarray, items: List["VisItem"]):
        for item in items:
            if item.mask is None:
                continue
            colored_mask = np.zeros_like(frame, dtype=np.uint8)
            colored_mask[item.mask > 0] = item.color
            frame[:] = cv2.addWeighted(frame, 0.5, colored_mask, 0.5, 0)
            contours, _ = cv2.findContours(
                item.mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            cv2.drawContours(frame, contours, -1, item.color, 2)
            if contours and item.label:
                x, y, _, _ = cv2.boundingRect(contours[0])
                cv2.putText(
                    frame, item.label, (x, y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.5, item.color, 1, cv2.LINE_AA,
                )

    def _draw_keypoints(self, frame: np.ndarray, items: List["VisItem"]):
        for item in items:
            if item.keypoints is None:
                continue
            for kp in item.keypoints:
                if isinstance(kp, (list, tuple)) and len(kp) >= 2:
                    x, y = int(kp[0]), int(kp[1])
                    cv2.circle(frame, (x, y), 5, item.color, -1)
                    cv2.circle(frame, (x, y), 6, (255, 255, 255), 1)
            if len(item.keypoints) > 1:
                for i in range(len(item.keypoints) - 1):
                    kp1, kp2 = item.keypoints[i], item.keypoints[i + 1]
                    if (isinstance(kp1, (list, tuple)) and len(kp1) >= 2
                            and isinstance(kp2, (list, tuple)) and len(kp2) >= 2):
                        cv2.line(
                            frame,
                            (int(kp1[0]), int(kp1[1])),
                            (int(kp2[0]), int(kp2[1])),
                            item.color, 2,
                        )

    def _draw_status_bar(
        self,
        frame: np.ndarray,
        text: str,
        color: tuple,
        position: str = "top-right",
    ):
        height, width = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale, thickness, padding = 0.6, 2, 10
        (text_w, text_h), _ = cv2.getTextSize(text, font, font_scale, thickness)
        if position == "top-right":
            x, y = width - text_w - padding, padding + text_h
        elif position == "top-left":
            x, y = padding, padding + text_h
        elif position == "bottom-right":
            x, y = width - text_w - padding, height - padding
        else:  # bottom-left
            x, y = padding, height - padding
        bg = 5
        cv2.rectangle(frame, (x - bg, y - text_h - bg), (x + text_w + bg, y + bg), (0, 0, 0), -1)
        cv2.putText(frame, text, (x, y), font, font_scale, color, thickness, cv2.LINE_AA)

    def _draw_global_info(
        self,
        frame: np.ndarray,
        stage: str,
        temporal_events: Optional[List[str]] = None,
    ):
        height, width = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale, thickness = 0.6, 2
        white = (255, 255, 255)
        cv2.putText(
            frame, f"Stage: {stage}", (10, height - 60),
            font, font_scale, white, thickness, cv2.LINE_AA,
        )
        cv2.putText(
            frame, datetime.now().strftime("%Y-%m-%d %H:%M:%S"), (10, height - 30),
            font, font_scale, white, thickness, cv2.LINE_AA,
        )
        if temporal_events:
            events_text = " | ".join(temporal_events[:2])
            (event_w, event_h), _ = cv2.getTextSize(events_text, font, font_scale, thickness)
            cv2.rectangle(
                frame,
                (width - event_w - 20, height - event_h - 20),
                (width - 10, height - 10),
                (0, 0, 0), -1,
            )
            cv2.putText(
                frame, events_text, (width - event_w - 15, height - 15),
                font, font_scale, (0, 165, 255), thickness, cv2.LINE_AA,
            )
