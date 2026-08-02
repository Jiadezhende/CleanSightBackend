"""Mock 检测：MockDetector（流源，纯 numpy 亮度启发式，无 YOLO 依赖）。

用于无真实模型权重的 CPU 服务器验证推理链路，亦作未知 step 的 MOCK 透传 fallback。
无状态，多 Client 共享，产出 "mock" 流。同业务点的时序算子见 temporal/impl/mock.py。
"""

from __future__ import annotations

from typing import List

import numpy as np

from app.services.inference.detection.detector import Detector
from app.domain.detection import Detection, FrameDetections
from app.domain.render import RenderItem, RenderSpec, RenderType

_MOCK_CLASS_ID = 0
_MOCK_CLASS_NAME = "mock_object"


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

    def _detect(self, frame: np.ndarray, timestamp: float) -> FrameDetections:
        """单帧亮度启发式检测。timestamp 为帧捕获真值锚点，由 infer_batch 穿入。"""
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

        return FrameDetections(
            detections=detections,
            metadata={
                "model": "mock_brightness",
                "mean_brightness": round(mean_brightness, 2),
            },
            timestamp=timestamp,
            success=True,
        )

    def infer_batch(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[FrameDetections]:
        return [self._detect(frame, ts) for frame, ts in zip(frames, timestamps)]

    def prepare_visualization_data(self, output: FrameDetections) -> RenderSpec:
        items = [
            RenderItem(
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

        return RenderSpec(
            type=RenderType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-left",
        )
