"""气泡检测任务"""

import logging
import time
from typing import Any, Dict, List

import numpy as np

from app.services.inference.workflows.infer_workflow import YOLOWorkflow
from app.services.inference.data_models import (
    AlarmInfo,
    DetectionOutput,
    VisualizationData,
    VisItem,
    VisualizationType,
)

logger = logging.getLogger(__name__)


class BubbleDetectionTask(YOLOWorkflow):
    """气泡检测任务

    时序逻辑：连续3帧检测到气泡才触发事件
    告警逻辑：触发事件时上报告警
    """

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.5,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(
            name="bubble",
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )

    # ====== 1. 检测（覆盖 infer_batch 以添加业务字段）======

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[DetectionOutput]:
        """批量气泡检测，利用 YOLO 批量推理接口"""
        try:
            outputs = self._run_yolo_batch(frames)
            for output in outputs:
                output.success = True
                output.bubble_detected = len(output.detections) > 0
                output.bubble_count = len(output.detections)
            return outputs
        except Exception as e:
            logger.error(f"[BubbleTask] Batch inference failed: {e}, fallback to single", exc_info=True)
            results = []
            for f, c in zip(frames, contexts):
                try:
                    output = self.infer(f, c)
                    output.bubble_detected = len(output.detections) > 0
                    output.bubble_count = len(output.detections)
                    results.append(output)
                except Exception as err:
                    results.append(DetectionOutput(
                        detections=[],
                        metadata={"error": str(err)},
                        timestamp=time.time(),
                        success=False,
                        error=str(err),
                    ))
            return results

    # ====== 2. 时序分析 ======

    def analyze_temporal(
        self, window: List[DetectionOutput]
    ) -> List[str]:
        """气泡时序分析：基于滑动窗口末尾连续检测帧数触发事件"""
        if not window:
            return []

        # 从窗口尾部计算连续检测帧数
        consecutive = 0
        for output in reversed(window):
            if len(output.detections) > 0:
                consecutive += 1
            else:
                break

        if consecutive >= 3:
            return [f"连续{consecutive}帧检测到气泡"]
        return []

    # ====== 3. 可视化数据准备 ======

    def prepare_visualization_data(
        self, output: DetectionOutput,
    ) -> VisualizationData:
        """准备气泡可视化数据"""
        items = []
        for det in output.detections:
            if det.class_name == "bubble_debug_box":
                color = (255, 0, 255)  # 洋红色（调试框）
            else:
                color = (0, 255, 255)  # 黄色（正常气泡）

            items.append(VisItem(
                bbox=det.bbox,
                label=f"{det.class_name} {det.confidence:.2f}",
                confidence=det.confidence,
                color=color,
            ))

        bubble_count = len(output.detections)
        detected = bubble_count > 0

        if detected:
            status_text = f"Bubbles: {bubble_count}"
            status_color = (0, 165, 255) if bubble_count > 5 else (0, 255, 255)
        else:
            status_text = "No Bubbles"
            status_color = (0, 255, 0)

        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-right",
        )

    # ====== 4. 告警评估 ======

    def evaluate_alarms(
        self, window: List[DetectionOutput], state,
    ) -> List[AlarmInfo]:
        """评估气泡告警：连续3帧触发，更新 ClientState 告警计数"""
        if not window:
            return []

        # 从窗口尾部计算连续检测帧数
        consecutive = 0
        for output in reversed(window):
            if len(output.detections) > 0:
                consecutive += 1
            else:
                break

        # 更新检测指标计数器
        latest = window[-1]
        if len(latest.detections) > 0:
            state.increment_counter("bubble_total", delta=len(latest.detections))

        if consecutive < 3:
            return []

        state.increment_counter("bubble_alarm_count")
        return [AlarmInfo(
            alarm_type="流程违规",
            alarm_level="high",
            alarm_message="检测到气泡异常（连续3帧）",
            metadata={
                "consecutive_frames": consecutive,
                "bubble_count": len(latest.detections),
            },
        )]
