"""内镜弯折检测任务"""

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
        self, window: List[DetectionOutput]
    ) -> List[str]:
        """弯折时序分析：基于滑动窗口内 window_seconds (2s) 子窗口，70% 比例触发事件"""
        if not window:
            return []

        # 取 slide_window 中最近 window_seconds (2s) 的子窗口
        latest_ts = window[-1].timestamp
        cutoff = latest_ts - self.window_seconds
        recent = [out for out in window if out.timestamp >= cutoff]

        bending_flags = [
            any(
                "bent" in d.class_name.lower() or "bending" in d.class_name.lower()
                for d in out.detections
            )
            for out in recent
        ]

        detected_ratio = sum(bending_flags) / len(bending_flags) if bending_flags else 0.0

        if detected_ratio >= self.trigger_ratio:
            return [f"滑动窗口内{detected_ratio:.0%}检测到弯折"]
        return []

    # ====== 3. 可视化数据准备 ======

    def prepare_visualization_data(
        self, output: DetectionOutput,
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

        bending_now = any(
            "bent" in d.class_name.lower() or "bending" in d.class_name.lower()
            for d in output.detections
        )

        if bending_now:
            status_text = "BENDING!"
            status_color = (0, 0, 255)
        else:
            status_text = "Normal"
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
        self, window: List[DetectionOutput], state,
    ) -> List[AlarmInfo]:
        """评估弯折告警：滑动窗口内70%触发，更新 ClientState 告警计数"""
        if not window:
            return []

        # 取最近 window_seconds 子窗口
        latest_ts = window[-1].timestamp
        cutoff = latest_ts - self.window_seconds
        recent = [out for out in window if out.timestamp >= cutoff]

        bending_flags = [
            any(
                "bent" in d.class_name.lower() or "bending" in d.class_name.lower()
                for d in out.detections
            )
            for out in recent
        ]

        detected_ratio = sum(bending_flags) / len(bending_flags) if bending_flags else 0.0

        # 更新检测指标计数器
        latest = window[-1]
        bending_now = any(
            "bent" in d.class_name.lower() or "bending" in d.class_name.lower()
            for d in latest.detections
        )
        if bending_now:
            state.increment_counter("bending_total")

        if detected_ratio < self.trigger_ratio:
            return []

        state.increment_counter("bending_alarm_count")
        return [AlarmInfo(
            alarm_type="流程违规",
            alarm_level="high",
            alarm_message="检测到内镜弯折异常（滑动窗口触发）",
            metadata={
                "window_ratio": detected_ratio,
                "bending_count": state.get_counter("bending_total", 0),
            },
        )]
