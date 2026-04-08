"""Mock 检测：MockDetector + MockAnalyzer

用于无真实模型权重的 CPU 服务器验证推理链路。

MockDetector（推理线程）：
    纯 numpy 亮度启发式检测，无 YOLO 依赖。
    无状态，多 Client 共享。

MockAnalyzer（时序线程）：
    连续 N 帧检测到目标 → 边沿触发告警。
    有状态，每个 Client 独立实例化。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Tuple

import numpy as np

from app.services.inference.workflows.detector import Detector
from app.services.inference.workflows.analyzer import TemporalAnalyzer
from app.services.inference.data_models import (
    AlarmInfo,
    AlarmType,
    Detection,
    DetectionOutput,
    VisualizationData,
    VisItem,
    VisualizationType,
)

logger = logging.getLogger(__name__)

_MOCK_CLASS_ID = 0
_MOCK_CLASS_NAME = "mock_object"


# ====== 推理线程：Detector ======

class MockDetector(Detector):
    """Mock 检测器（纯 numpy，无模型）。无状态，多 Client 共享。

    取帧中心 1/4 区域灰度均值，均值 < brightness_threshold 视为检测到目标。
    """

    def __init__(
        self,
        brightness_threshold: float = 100.0,
        enabled: bool = True,
    ):
        super().__init__(name="mock", enabled=enabled)
        self.brightness_threshold = brightness_threshold

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        timestamp = time.time()
        h, w = frame.shape[:2]
        cy1, cy2 = h // 4, 3 * h // 4
        cx1, cx2 = w // 4, 3 * w // 4
        center_crop = frame[cy1:cy2, cx1:cx2]

        gray = np.mean(center_crop, axis=2) if center_crop.ndim == 3 else center_crop.astype(float)
        mean_brightness = float(np.mean(gray))

        detections: List[Detection] = []
        if mean_brightness < self.brightness_threshold:
            confidence = 1.0 - mean_brightness / 255.0
            detections.append(Detection(
                bbox=[cx1, cy1, cx2, cy2],
                confidence=round(confidence, 4),
                class_id=_MOCK_CLASS_ID,
                class_name=_MOCK_CLASS_NAME,
                extra={"mean_brightness": round(mean_brightness, 2)},
            ))

        return DetectionOutput(
            detections=detections,
            metadata={
                "model": "mock_brightness",
                "frame_shape": frame.shape,
                "mean_brightness": round(mean_brightness, 2),
            },
            timestamp=timestamp,
            success=True,
        )

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[DetectionOutput]:
        return [self.infer(frame, ctx) for frame, ctx in zip(frames, contexts)]

    def prepare_visualization_data(self, output: DetectionOutput) -> VisualizationData:
        items = [
            VisItem(
                bbox=det.bbox,
                label=f"[MOCK] {det.confidence:.2f}",
                confidence=det.confidence,
                color=(255, 128, 0),
            )
            for det in output.detections
        ]

        detected = len(output.detections) > 0
        brightness = output.metadata.get("mean_brightness", "-")

        if detected:
            status_text = f"[MOCK] Detected (lum={brightness})"
            status_color = (0, 128, 255)
        else:
            status_text = f"[MOCK] Clear (lum={brightness})"
            status_color = (0, 200, 0)

        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-left",
        )


# ====== 时序线程：TemporalAnalyzer ======

class MockAnalyzer(TemporalAnalyzer):
    """Mock 时序分析器。有状态，每个 Client 独立实例化。

    状态机（self._sm）：
        alarming: 上升沿锁存
        total: 累计检测到的目标数
        alarm_count: 累计告警次数
    """

    def __init__(self, consecutive_trigger: int = 3):
        super().__init__(name="mock")
        self.consecutive_trigger = consecutive_trigger
        self._sm = {
            "alarming": False,
            "total": 0,
            "alarm_count": 0,
        }

    def analyze_temporal(
        self, window: List[DetectionOutput],
    ) -> Tuple[List[str], List[AlarmInfo]]:
        if not window:
            return [], []

        # 从最新帧往前统计连续检测到目标的帧数
        consecutive = 0
        for output in reversed(window):
            if len(output.detections) > 0:
                consecutive += 1
            else:
                break

        latest = window[-1]
        if len(latest.detections) > 0:
            self._sm["total"] += len(latest.detections)

        is_triggered = consecutive >= self.consecutive_trigger
        events = (
            [f"mock_object detected in {consecutive} consecutive frames"]
            if is_triggered else []
        )
        alarms: List[AlarmInfo] = []

        if is_triggered and not self._sm["alarming"]:
            self._sm["alarming"] = True
            self._sm["alarm_count"] += 1
            alarms.append(AlarmInfo(
                alarm_type=AlarmType.MOCK,
                alarm_level="low",
                alarm_message=f"Mock detection triggered ({consecutive} consecutive frames)",
                metadata={
                    "consecutive_frames": consecutive,
                    "brightness": latest.metadata.get("mean_brightness"),
                },
            ))
        elif not is_triggered and self._sm["alarming"]:
            self._sm["alarming"] = False

        return events, alarms
