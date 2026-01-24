"""
默认可视化器实现
"""

import random
from datetime import datetime
from typing import Any, Dict, Optional

import cv2
import numpy as np

from app.services.inference.models import TemporalAnalysisResult
from app.services.inference.workers.visualization import Visualizer
from app.settings import settings


class DefaultVisualizer(Visualizer):
    """默认可视化器：绘制检测框、标注和文字信息。

    参考：app/services/task_pipeline/leak/leak_test.py 中的可视化实现
    """

    def __init__(self):
        """初始化可视化器，为每个模型生成固定的随机位置和颜色。"""
        self.debug_boxes: Dict[str, tuple] = {}  # 存储每个模型的调试框信息 {model_name: (x, y, color)}
        random.seed(42)  # 设置随机种子，确保每次运行时位置相同

    def visualize(
        self,
        frame: np.ndarray,
        inference_result: Dict[str, Any],
        stage: str,
        temporal_result: Optional[TemporalAnalysisResult] = None,
    ) -> np.ndarray:
        """在帧上绘制检测结果和标注。

        Args:
            frame: 原始帧
            inference_result: 推理结果（各子任务的输出）
            stage: 当前阶段（LEAK/CLEAN）
            temporal_result: 时序分析结果（可选）

        Returns:
            可视化后的帧
        """
        if frame is None:
            return frame

        # 复制帧，避免修改原始数据
        annotated = frame.copy()

        # 1. 绘制检测框（根据不同子任务）
        for subtask_name, subtask_res in inference_result.items():
            if not isinstance(subtask_res, dict):
                continue

            # 气泡检测可视化
            if subtask_name == "bubble":
                self._draw_bubble_detection(annotated, subtask_res)

            # 弯折检测可视化
            elif subtask_name == "bending":
                self._draw_bending_detection(annotated, subtask_res)

            # 其他子任务可扩展
            # elif subtask_name == "quality":
            #     self._draw_quality_detection(annotated, subtask_res)

        # 2. 绘制文字信息（stage、timestamp、fps等）
        self._draw_text_info(annotated, stage, temporal_result)

        # 3. 绘制调试方框（用于确认模型是否加载）- 可通过环境变量控制
        if settings.show_debug_boxes:
            self._draw_debug_boxes(annotated, inference_result)

        return annotated

    def _draw_bubble_detection(self, frame: np.ndarray, result: Dict[str, Any]):
        """绘制气泡检测结果"""
        detected = result.get("bubble_detected", False)
        confidence = result.get("confidence", 0.0)
        boxes = result.get("boxes", [])

        # 绘制检测框
        for box in boxes:
            if isinstance(box, (list, tuple)) and len(box) >= 4:
                x1, y1, x2, y2 = map(int, box[:4])
                color = (0, 0, 255) if detected else (0, 255, 0)  # 红色=检测到，绿色=未检测到
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        # 绘制标签
        if detected:
            label = f"Bubble: {confidence:.2f}"
            cv2.putText(
                frame,
                label,
                (10, 30),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 0, 255),
                2,
            )

    def _draw_bending_detection(self, frame: np.ndarray, result: Dict[str, Any]):
        """绘制弯折检测结果"""
        detected = result.get("bending_detected", False)
        confidence = result.get("confidence", 0.0)
        boxes = result.get("boxes", [])

        # 绘制检测框
        for box in boxes:
            if isinstance(box, (list, tuple)) and len(box) >= 4:
                x1, y1, x2, y2 = map(int, box[:4])
                color = (255, 0, 0) if detected else (0, 255, 0)  # 蓝色=检测到，绿色=未检测到
                cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

        # 绘制标签
        if detected:
            label = f"Bending: {confidence:.2f}"
            cv2.putText(
                frame,
                label,
                (10, 60),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (255, 0, 0),
                2,
            )

    def _draw_text_info(
        self,
        frame: np.ndarray,
        stage: str,
        temporal_result: Optional[TemporalAnalysisResult] = None,
    ):
        """绘制文字信息（stage、时间戳、事件等）"""
        # Stage 信息
        stage_text = f"Stage: {stage}"
        cv2.putText(
            frame,
            stage_text,
            (10, frame.shape[0] - 60),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
        )

        # 时间戳
        timestamp_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(
            frame,
            timestamp_text,
            (10, frame.shape[0] - 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (255, 255, 255),
            2,
        )

        # 时序事件
        if temporal_result and temporal_result.events:
            event_text = " | ".join(temporal_result.events[:2])  # 最多显示2个事件
            cv2.putText(
                frame,
                event_text,
                (10, frame.shape[0] - 90),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.5,
                (0, 255, 255),
                1,
            )

    def _draw_debug_boxes(self, frame: np.ndarray, inference_result: Dict[str, Any]):
        """为每个模型绘制调试方框（用于确认模型是否加载）。

        Args:
            frame: 帧图像
            inference_result: 推理结果，键为模型名称
        """
        if frame is None or len(frame.shape) < 2:
            return

        h, w = frame.shape[:2]
        box_size = 80  # 方框大小

        # 预定义颜色列表（BGR格式）
        colors = [
            (255, 0, 0),    # 蓝色
            (0, 255, 0),    # 绿色
            (0, 0, 255),    # 红色
            (255, 255, 0),  # 青色
            (255, 0, 255),  # 品红
            (0, 255, 255),  # 黄色
            (128, 0, 128),  # 紫色
            (255, 128, 0),  # 橙色
        ]

        # 为每个模型绘制调试框
        for idx, (model_name, model_result) in enumerate(inference_result.items()):
            if not isinstance(model_result, dict):
                continue

            # 如果该模型还没有分配调试框位置，则生成一个
            if model_name not in self.debug_boxes:
                # 生成随机位置（确保不超出边界）
                x = random.randint(box_size, max(box_size + 1, w - box_size - 100))
                y = random.randint(box_size, max(box_size + 1, h - box_size - 50))
                color = colors[idx % len(colors)]
                self.debug_boxes[model_name] = (x, y, color)

            # 获取该模型的调试框信息
            x, y, color = self.debug_boxes[model_name]

            # 绘制方框
            cv2.rectangle(
                frame,
                (x - box_size // 2, y - box_size // 2),
                (x + box_size // 2, y + box_size // 2),
                color,
                3,
            )

            # 绘制标签文字
            label = f"debug:{model_name}"
            font_scale = 0.5
            thickness = 2

            # 获取文字大小
            (text_w, text_h), baseline = cv2.getTextSize(
                label, cv2.FONT_HERSHEY_SIMPLEX, font_scale, thickness
            )

            # 绘制文字背景
            cv2.rectangle(
                frame,
                (x - box_size // 2, y - box_size // 2 - text_h - baseline - 5),
                (x - box_size // 2 + text_w + 5, y - box_size // 2),
                color,
                -1,
            )

            # 绘制文字
            cv2.putText(
                frame,
                label,
                (x - box_size // 2 + 2, y - box_size // 2 - 5),
                cv2.FONT_HERSHEY_SIMPLEX,
                font_scale,
                (255, 255, 255),
                thickness,
            )
