"""固定可视化渲染器

根据 Task 提供的 VisualizationData 渲染视频帧，无需针对每个任务编写可视化代码。
支持多种可视化类型：BBox、Segmentation、Keypoint
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

import cv2
import numpy as np

from app.services.inference.data_models import VisualizationData, VisItem, VisualizationType
from app.services.inference.models import TemporalAnalysisResult

logger = logging.getLogger(__name__)


class FixedVisualizer:
    """固定可视化渲染器
    
    根据 Task 提供的 VisualizationData 渲染，无需子类化。
    Task 只需准备数据（检测框、标签、颜色等），渲染逻辑由本类统一处理。
    """

    def render(
        self,
        frame: np.ndarray,
        vis_data_list: List[VisualizationData],
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

        # 复制帧，避免修改原始数据
        annotated = frame.copy()

        # 1. 渲染每个Task的检测结果
        for vis_data in vis_data_list:
            if vis_data.type == VisualizationType.BBOX:
                self._draw_bboxes(annotated, vis_data.items)
            elif vis_data.type == VisualizationType.MASK:
                self._draw_masks(annotated, vis_data.items)
            elif vis_data.type == VisualizationType.KEYPOINT:
                self._draw_keypoints(annotated, vis_data.items)
            
            # 绘制状态栏
            self._draw_status_bar(
                annotated,
                vis_data.status_text,
                vis_data.status_color,
                vis_data.status_position
            )

        # 2. 绘制全局信息（Stage、时间戳、事件）
        self._draw_global_info(annotated, stage, temporal_events)

        return annotated

    def _draw_bboxes(self, frame: np.ndarray, items: List[VisItem]):
        """绘制检测框（BBox类型）

        Args:
            frame: 图像帧
            items: 可视化项列表
        """
        for item in items:
            if item.bbox is None:
                continue

            x1, y1, x2, y2 = item.bbox
            color = item.color

            # 绘制边界框
            cv2.rectangle(frame, (x1, y1), (x2, y2), color, 2)

            # 绘制标签（如果有）
            if item.label:
                (label_w, label_h), baseline = cv2.getTextSize(
                    item.label, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1
                )
                label_y = max(y1 - 10, label_h + 5)

                # 标签背景
                cv2.rectangle(
                    frame,
                    (x1, label_y - label_h - 5),
                    (x1 + label_w + 6, label_y + 2),
                    color,
                    -1,
                )

                # 标签文字（黑色）
                cv2.putText(
                    frame,
                    item.label,
                    (x1 + 3, label_y - 3),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    (0, 0, 0),
                    1,
                    cv2.LINE_AA,
                )

    def _draw_masks(self, frame: np.ndarray, items: List[VisItem]):
        """绘制分割掩码（Mask类型）

        Args:
            frame: 图像帧
            items: 可视化项列表
        """
        for item in items:
            if item.mask is None:
                continue

            # 创建彩色掩码
            colored_mask = np.zeros_like(frame, dtype=np.uint8)
            colored_mask[item.mask > 0] = item.color

            # 半透明叠加
            alpha = 0.5
            frame[:] = cv2.addWeighted(frame, 1 - alpha, colored_mask, alpha, 0)

            # 绘制轮廓
            contours, _ = cv2.findContours(
                item.mask.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE
            )
            cv2.drawContours(frame, contours, -1, item.color, 2)

            # 绘制标签（在mask边界框上）
            if contours and item.label:
                x, y, w, h = cv2.boundingRect(contours[0])
                cv2.putText(
                    frame,
                    item.label,
                    (x, y - 10),
                    cv2.FONT_HERSHEY_SIMPLEX,
                    0.5,
                    item.color,
                    1,
                    cv2.LINE_AA,
                )

    def _draw_keypoints(self, frame: np.ndarray, items: List[VisItem]):
        """绘制关键点（Keypoint类型）

        Args:
            frame: 图像帧
            items: 可视化项列表
        """
        for item in items:
            if item.keypoints is None:
                continue

            # 绘制关键点
            for kp in item.keypoints:
                if isinstance(kp, (list, tuple)) and len(kp) >= 2:
                    x, y = int(kp[0]), int(kp[1])
                    cv2.circle(frame, (x, y), 5, item.color, -1)
                    cv2.circle(frame, (x, y), 6, (255, 255, 255), 1)

            # 绘制连线（如果关键点数>1）
            if len(item.keypoints) > 1:
                for i in range(len(item.keypoints) - 1):
                    kp1, kp2 = item.keypoints[i], item.keypoints[i + 1]
                    if (isinstance(kp1, (list, tuple)) and len(kp1) >= 2 and
                        isinstance(kp2, (list, tuple)) and len(kp2) >= 2):
                        pt1 = (int(kp1[0]), int(kp1[1]))
                        pt2 = (int(kp2[0]), int(kp2[1]))
                        cv2.line(frame, pt1, pt2, item.color, 2)

    def _draw_status_bar(
        self,
        frame: np.ndarray,
        text: str,
        color: tuple,
        position: str = "top-right"
    ):
        """绘制状态栏
        
        Args:
            frame: 图像帧
            text: 状态文本
            color: 文本颜色（BGR）
            position: 位置 ("top-left", "top-right", "bottom-left", "bottom-right")
        """
        height, width = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.6
        thickness = 2
        padding = 10

        # 获取文本尺寸
        (text_w, text_h), baseline = cv2.getTextSize(text, font, font_scale, thickness)

        # 根据位置计算坐标
        if position == "top-right":
            x = width - text_w - padding
            y = padding + text_h
        elif position == "top-left":
            x = padding
            y = padding + text_h
        elif position == "bottom-right":
            x = width - text_w - padding
            y = height - padding
        else:  # bottom-left
            x = padding
            y = height - padding

        # 绘制背景矩形
        bg_padding = 5
        cv2.rectangle(
            frame,
            (x - bg_padding, y - text_h - bg_padding),
            (x + text_w + bg_padding, y + bg_padding),
            (0, 0, 0),
            -1,
        )

        # 绘制文本
        cv2.putText(
            frame, text, (x, y), font, font_scale, color, thickness, cv2.LINE_AA
        )

    def _draw_global_info(
        self,
        frame: np.ndarray,
        stage: str,
        temporal_events: Optional[List[str]] = None
    ):
        """绘制全局信息（Stage、时间戳、事件等）
        
        Args:
            frame: 图像帧
            stage: 当前阶段
            temporal_events: 时序事件列表
        """
        height, width = frame.shape[:2]
        font = cv2.FONT_HERSHEY_SIMPLEX
        font_scale = 0.6
        thickness = 2
        color = (255, 255, 255)  # 白色

        # Stage 信息（左下角）
        stage_text = f"Stage: {stage}"
        cv2.putText(
            frame,
            stage_text,
            (10, height - 60),
            font,
            font_scale,
            color,
            thickness,
            cv2.LINE_AA,
        )

        # 时间戳（左下角）
        timestamp_text = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        cv2.putText(
            frame,
            timestamp_text,
            (10, height - 30),
            font,
            font_scale,
            color,
            thickness,
            cv2.LINE_AA,
        )

        # 时序事件（右下角）
        if temporal_events:
            events_text = " | ".join(temporal_events[:2])  # 最多显示2个事件
            (event_w, event_h), _ = cv2.getTextSize(events_text, font, font_scale, thickness)
            
            # 绘制事件背景
            cv2.rectangle(
                frame,
                (width - event_w - 20, height - event_h - 20),
                (width - 10, height - 10),
                (0, 0, 0),
                -1,
            )
            
            # 绘制事件文本（橙色）
            cv2.putText(
                frame,
                events_text,
                (width - event_w - 15, height - 15),
                font,
                font_scale,
                (0, 165, 255),  # 橙色
                thickness,
                cv2.LINE_AA,
            )
