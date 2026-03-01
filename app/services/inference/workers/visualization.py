"""visualization.py - 可视化工作线程池（定时拉取架构）。

职责：
- 按固定间隔（tick_interval）轮询所有活跃客户端
- 从 ClientQueues 主动拉取三要素：
  - cq.get_latest_inference()  → 原子推理快照（所有 task 同帧一致）
  - cq.get_latest_frame()      → 最新原始帧
  - cq.get_latest_temporal()   → 最新时序事件
- 绘制检测框、标注、文字信息到最新帧上
- 写回 ClientQueues（ca_processed + _latest_rendered）
"""

import logging
import threading
import time
from datetime import datetime
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from app.services.inference.data_models import (
    DetectionOutput,
    VisualizationData,
    VisItem,
    VisualizationType,
)

from app.models.frame import FrameData
from app.services.client import client_manager
from app.services.inference.models import InferenceResult

logger = logging.getLogger(__name__)


class VisualizationWorker:
    """可视化工作线程（定时拉取模式）。

    独立于 TemporalWorker，按自己的节奏（tick_interval）遍历所有活跃客户端，
    从 ClientQueues 拉取原子推理快照 + 最新帧 + 时序事件，渲染后写回。
    """

    def __init__(
        self,
        stop_event: threading.Event,
        tick_interval: float = 1.0 / 15,  # ~15 FPS
        worker_id: int = 0,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化可视化工作线程。

        Args:
            stop_event: 停止事件
            tick_interval: 拉取间隔（秒），默认 ~15 FPS
            worker_id: 工作线程ID（用于调试）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.stop_event = stop_event
        self.tick_interval = tick_interval
        self.worker_id = worker_id
        self.stage_configs = stage_configs or {}
        self.fixed_visualizer = FixedVisualizer()

        # 去重：记录每个客户端上次渲染的推理时间戳，避免重复渲染同一帧
        self._last_rendered_ts: Dict[str, float] = {}

    def run(self):
        """工作循环：固定间隔轮询所有客户端。"""
        logger.debug(
            "[VisualizationWorker-%d] Started (tick=%.3fs, ~%.0f FPS)",
            self.worker_id, self.tick_interval, 1.0 / self.tick_interval,
        )

        while not self.stop_event.is_set():
            tick_start = time.time()
            try:
                self._tick()
            except Exception as e:
                logger.error(
                    "[VisualizationWorker-%d] Tick exception: %s",
                    self.worker_id, e, exc_info=True,
                )

            # 睡眠至下一个 tick
            elapsed = time.time() - tick_start
            sleep_time = max(0, self.tick_interval - elapsed)
            if sleep_time > 0:
                self.stop_event.wait(sleep_time)

        logger.debug("[VisualizationWorker-%d] Stopped", self.worker_id)

    def _tick(self):
        """一次轮询：遍历所有活跃客户端执行可视化。"""
        all_clients = client_manager.get_all_clients()
        for client_id, cq in all_clients.items():
            try:
                self._process_client(client_id, cq)
            except Exception as e:
                logger.error(
                    "[VisualizationWorker-%d] Error processing client %s: %s",
                    self.worker_id, client_id, e, exc_info=True,
                )

    def _process_client(self, client_id: str, cq) -> None:
        """处理单个客户端的可视化。"""
        # 1. 原子读取推理快照（所有 task 同帧一致）
        inference: Optional[InferenceResult] = cq.get_latest_inference()
        if inference is None:
            return

        # 2. 去重：跳过已渲染过的同一推理结果
        last_ts = self._last_rendered_ts.get(client_id, 0.0)
        if inference.timestamp <= last_ts:
            return

        # 3. 获取最新原始帧
        frame = cq.get_latest_frame()
        if frame is None:
            return

        # 4. 获取最新时序事件
        events = cq.get_latest_temporal()

        # 5. 渲染
        stage = inference.stage
        annotated_frame = self._render(frame, stage, inference.result, events)

        # 6. 写回
        frame_data = FrameData(
            timestamp=inference.timestamp,
            frame=annotated_frame,
            inference_result=inference.result,
        )
        cq.append_ca_processed(frame_data)
        cq.set_latest_rendered(frame_data)

        # 7. 更新去重时间戳
        self._last_rendered_ts[client_id] = inference.timestamp

    def _render(
        self,
        frame: np.ndarray,
        stage: str,
        detection_results: Dict[str, DetectionOutput],
        events: List[str],
    ) -> np.ndarray:
        """使用固定渲染器进行可视化。

        Args:
            frame: 原始帧
            stage: 当前阶段
            detection_results: 推理结果 {task_name: DetectionOutput}（同帧原子快照）
            events: 时序事件列表
        """
        try:
            # 获取当前 stage 的 tasks（用于调用 prepare_visualization_data）
            stage_cfg = self.stage_configs.get(stage, {})
            tasks = stage_cfg.get("models", [])

            if not tasks:
                return frame.copy()

            vis_data_list: List[VisualizationData] = []

            for task in tasks:
                detection_output = detection_results.get(task.name)

                if not isinstance(detection_output, DetectionOutput):
                    continue

                vis_data = task.prepare_visualization_data(detection_output)
                vis_data_list.append(vis_data)

            # 使用固定渲染器渲染
            annotated_frame = self.fixed_visualizer.render(
                frame=frame.copy(),
                vis_data_list=vis_data_list,
                stage=stage,
                temporal_events=events,
            )

            return annotated_frame

        except Exception as e:
            logger.error("[VisualizationWorker] Render failed: %s", e, exc_info=True)
            return frame.copy()


class VisualizationWorkerPool:
    """可视化线程池（定时拉取模式，单线程）。

    单线程理由：单帧渲染 ~5ms，15 FPS × 10 clients = 50ms/67ms，单线程足够。
    单线程避免了多线程竞争同一客户端的问题。
    """

    def __init__(
        self,
        target_fps: float = 15,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    ):
        """初始化可视化线程池。

        Args:
            target_fps: 目标可视化帧率（默认 15 FPS）
            stage_configs: Stage 配置字典 {stage_name: {"models": [tasks]}}
        """
        self.target_fps = target_fps
        self.stage_configs = stage_configs or {}

        self._stop_event = threading.Event()
        self._worker_thread: Optional[threading.Thread] = None

    def start(self):
        """启动工作线程。"""
        tick_interval = 1.0 / self.target_fps

        worker = VisualizationWorker(
            stop_event=self._stop_event,
            tick_interval=tick_interval,
            worker_id=0,
            stage_configs=self.stage_configs,
        )

        self._worker_thread = threading.Thread(
            target=worker.run,
            daemon=True,
            name="VisualizationWorker-0",
        )
        self._worker_thread.start()

        logger.info(
            "[VisualizationWorkerPool] Started (target_fps=%.0f, tick=%.3fs)",
            self.target_fps, tick_interval,
        )

    def stop(self):
        """停止工作线程。"""
        self._stop_event.set()

        if self._worker_thread is not None:
            self._worker_thread.join(timeout=2.0)

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
