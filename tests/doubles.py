"""测试替身：无权重、无 torch 的 Detector / OfflineSegmenter（生产配置与代码里没有 MOCK）。

    MockDetector         纯 numpy 亮度启发式 Detector（中心区灰度均值 < 阈值即出框）
    BrushRulesSegmenter  纯规则离线分段（任一 source 有框即 active，连续 active 帧并段）

测试 config 里按模块名引用：`{"class": "doubles.BrushRulesSegmenter"}`（tests/ 在 sys.path 上）。
"""

from __future__ import annotations

from typing import Any, List, Sequence

import numpy as np

from app.domain.detection import DetBox, DetectorOutput, FrameDetection
from app.domain.render import RenderItem, RenderSpec, RenderType
from app.domain.temporal import TemporalSegment
from app.services.inference.offline.segmenter import OfflineSegmenter
from app.services.inference.online.detection.detector import Detector

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

    def _detect(self, frame: np.ndarray, timestamp: float) -> DetectorOutput:
        """单帧亮度启发式检测。timestamp 为帧捕获真值锚点，由 infer_batch 穿入。"""
        h, w = frame.shape[:2]
        cy1, cy2 = h // 4, 3 * h // 4
        cx1, cx2 = w // 4, 3 * w // 4
        center_crop = frame[cy1:cy2, cx1:cx2]

        gray = np.mean(center_crop, axis=2) if center_crop.ndim == 3 else center_crop.astype(float)
        mean_brightness = float(np.mean(gray))

        detections: List[DetBox] = []
        if mean_brightness < self.brightness_threshold:
            confidence = 1.0 - mean_brightness / 255.0
            detections.append(DetBox(
                bbox=[cx1, cy1, cx2, cy2],
                confidence=round(confidence, 4),
                class_id=_MOCK_CLASS_ID,
                class_name=_MOCK_CLASS_NAME,
                extra={"mean_brightness": round(mean_brightness, 2)},
            ))

        return DetectorOutput(
            boxes=detections,
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
    ) -> List[DetectorOutput]:
        return [self._detect(frame, ts) for frame, ts in zip(frames, timestamps)]

    def prepare_visualization_data(self, output: DetectorOutput) -> RenderSpec:
        items = [
            RenderItem(
                bbox=det.bbox,
                label=f"[MOCK] {det.confidence:.2f}",
                confidence=det.confidence,
                color=(255, 128, 0),
            )
            for det in output.boxes
        ]

        detected = len(output.boxes) > 0
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


class BrushRulesSegmenter(OfflineSegmenter):
    """纯规则 Mock 分段器。

    Args:
        label: active 片段写出的动作标签，默认 `mock_action`。
        min_frames: 一个片段至少包含多少个 active 采样帧。
    """

    def __init__(self, label: str = "mock_action", min_frames: int = 1):
        self.label = label
        self.min_frames = max(1, int(min_frames))

    def preprocess(self, frames: Sequence[FrameDetection]) -> Sequence[FrameDetection]:
        """Mock 不做特征工程，直接把帧序列交给规则逻辑。"""
        return frames

    def segment(self, model_input: Any) -> List[TemporalSegment]:
        frames: Sequence[FrameDetection] = model_input
        segments: List[TemporalSegment] = []
        run_start: float | None = None
        run_last = 0.0
        run_count = 0
        for ff in frames:  # load 已按 ts 升序
            active = any(fd.boxes for fd in ff.by_source.values())
            if active:
                if run_start is None:
                    run_start = ff.ts
                    run_count = 0
                run_last = ff.ts
                run_count += 1
                continue

            if run_start is not None and run_count >= self.min_frames:
                segments.append(self._make(run_start, run_last))
            run_start = None
            run_count = 0

        if run_start is not None and run_count >= self.min_frames:
            segments.append(self._make(run_start, run_last))
        return segments

    def _make(self, start: float, end: float) -> TemporalSegment:
        return TemporalSegment(
            producer=self.name,
            label=self.label,
            start=float(start),
            end=float(end),
            conf=1.0,
            meta={"model_version": "brush_rules_v1"},
        )
