"""气泡检测任务"""

import logging
import time
from typing import Any, Dict, List

import numpy as np

from app.services.inference.workflows.infer_workflow import YOLOWorkflow
from app.services.inference.data_models import (
    AlarmInfo,
    DetectionOutput,
    TemporalResult,
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
        self, state, output: DetectionOutput, timestamp: float
    ) -> TemporalResult:
        """气泡时序分析：连续3帧检测到才触发事件"""
        bubble_count = len(output.detections)
        detected = bubble_count > 0

        if detected:
            consecutive = state.increment_counter("bubble_consecutive")
        else:
            state.reset_counter("bubble_consecutive")
            consecutive = 0

        if detected:
            total = state.increment_counter("bubble_total", delta=bubble_count)
        else:
            total = state.get_counter("bubble_total", 0)

        event_triggered = consecutive >= 3
        event_message = f"连续{consecutive}帧检测到气泡" if event_triggered else None

        return TemporalResult(
            detected=detected,
            event_triggered=event_triggered,
            event_message=event_message,
            counters={
                "bubble_count": bubble_count,
                "consecutive_frames": consecutive,
                "total_bubbles": total,
            },
        )

    # ====== 3. 可视化数据准备 ======

    def prepare_visualization_data(
        self, output: DetectionOutput, temporal: TemporalResult
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

        bubble_count = temporal.counters.get("bubble_count", 0)
        total = temporal.counters.get("total_bubbles", 0)

        if temporal.detected:
            status_text = f"Bubbles: {bubble_count} (Total: {total})"
            status_color = (0, 165, 255) if bubble_count > 5 else (0, 255, 255)
        else:
            status_text = f"No Bubbles (Total: {total})"
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
        self, temporal: TemporalResult, context: Dict[str, Any]
    ) -> List[AlarmInfo]:
        """评估气泡告警：连续3帧触发"""
        if not temporal.event_triggered:
            return []
        return [AlarmInfo(
            alarm_type="流程违规",
            alarm_level="high",
            alarm_message="检测到气泡异常（连续3帧）",
            metadata={
                "consecutive_frames": temporal.counters.get("consecutive_frames", 0),
                "bubble_count": temporal.counters.get("bubble_count", 0),
            },
        )]
