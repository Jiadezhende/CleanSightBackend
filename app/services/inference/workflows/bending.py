"""内镜弯折检测任务"""

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


class EndoscopeBendingDetectionTask(YOLOWorkflow):
    """内镜弯折检测任务

    时序逻辑：滑动窗口2秒，70%比例触发事件
    告警逻辑：触发事件时上报告警
    """

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.6,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(
            name="bending",
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )
        self.window_seconds = 2.0
        self.trigger_ratio = 0.7

    # ====== 1. 检测（覆盖 infer_batch 以添加业务字段）======

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[DetectionOutput]:
        """批量弯折检测，利用 YOLO 批量推理接口"""
        try:
            outputs = self._run_yolo_batch(frames)
            for output in outputs:
                output.success = True
                output.bending_detected = any(
                    "bent" in d.class_name.lower() for d in output.detections
                )
                output.detection_count = len(output.detections)
            return outputs
        except Exception as e:
            logger.error(f"[BendingTask] Batch inference failed: {e}, fallback to single", exc_info=True)
            results = []
            for f, c in zip(frames, contexts):
                try:
                    output = self.infer(f, c)
                    output.bending_detected = any(
                        "bent" in d.class_name.lower() for d in output.detections
                    )
                    output.detection_count = len(output.detections)
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
        """弯折时序分析：滑动窗口2秒，70%比例触发事件"""
        bending_detected = any(
            "bent" in det.class_name.lower() or "bending" in det.class_name.lower()
            for det in output.detections
        )

        state.push_temporal_history(
            "bending_window", bending_detected, timestamp, window_seconds=self.window_seconds
        )
        window_values = state.get_temporal_values(
            "bending_window", timestamp, self.window_seconds
        )

        detected_ratio = sum(window_values) / len(window_values) if window_values else 0
        event_triggered = detected_ratio >= self.trigger_ratio

        if bending_detected:
            count = state.increment_counter("bending_total")
        else:
            count = state.get_counter("bending_total", 0)

        event_message = f"滑动窗口内{detected_ratio:.0%}检测到弯折" if event_triggered else None

        return TemporalResult(
            detected=bending_detected,
            event_triggered=event_triggered,
            event_message=event_message,
            counters={
                "bending_count": count,
                "window_ratio": detected_ratio,
                "window_size": len(window_values),
            },
        )

    # ====== 3. 可视化数据准备 ======

    def prepare_visualization_data(
        self, output: DetectionOutput, temporal: TemporalResult
    ) -> VisualizationData:
        """准备弯折可视化数据"""
        items = []
        for det in output.detections:
            is_bending = "bent" in det.class_name.lower() or "bending" in det.class_name.lower()

            if det.class_name == "bending_debug_box":
                color = (255, 0, 255)  # 洋红色（调试框）
            elif is_bending:
                color = (0, 0, 255)    # 红色（弯折）
            else:
                color = (0, 255, 0)    # 绿色（正常）

            items.append(VisItem(
                bbox=det.bbox,
                label=f"{det.class_name} {det.confidence:.2f}",
                confidence=det.confidence,
                color=color,
            ))

        count = temporal.counters.get("bending_count", 0)

        if temporal.detected:
            status_text = f"BENDING! Count: {count}"
            status_color = (0, 0, 255)
        else:
            status_text = f"Normal (Count: {count})"
            status_color = (0, 255, 0)

        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-left",
        )

    # ====== 4. 告警评估 ======

    def evaluate_alarms(
        self, temporal: TemporalResult, context: Dict[str, Any]
    ) -> List[AlarmInfo]:
        """评估弯折告警：滑动窗口内70%触发"""
        if not temporal.event_triggered:
            return []
        return [AlarmInfo(
            alarm_type="流程违规",
            alarm_level="high",
            alarm_message="检测到内镜弯折异常（滑动窗口触发）",
            metadata={
                "window_ratio": temporal.counters.get("window_ratio", 0),
                "bending_count": temporal.counters.get("bending_count", 0),
            },
        )]
