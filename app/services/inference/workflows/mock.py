"""Mock 检测：MockDetector + MockAnalyzer（L3 产事实）+ MockJudge（L4 出告警）

用于无真实模型权重的 CPU 服务器验证推理链路。

MockDetector（推理线程）：
    纯 numpy 亮度启发式检测，无 YOLO 依赖。
    无状态，多 Client 共享。

MockAnalyzer（时序线程，L3）：
    统计连续命中帧数（consecutive），只产 EventFact("mock","consecutive",n)，不判定。
    有状态，每个 Client 独立实例化。

MockJudge（时序线程，L4）：
    持 consecutive_trigger，连续 N 帧命中 → 边沿触发告警（上升沿锁存）。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Tuple

import numpy as np

from app.services.inference.workflows.detector import Detector
from app.services.inference.workflows.analyzer import TemporalAnalyzer
from app.services.inference.workflows.judge import Judge
from app.services.inference.data_models import (
    AlarmInfo,
    AlarmType,
    Detection,
    DetectionOutput,
    EventFact,
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


# ====== 时序线程 L3：TemporalAnalyzer（只产事实）======

class MockAnalyzer(TemporalAnalyzer):
    """Mock 时序分析器（L3）。有状态，每个 Client 独立实例化。

    测量状态机（self._sm）：
        last_ts: 游标，已处理到的最新帧 timestamp（跨 tick 跳过重复帧）
        consecutive: 连续命中帧计数（跨 tick 累积，命中 +1，未命中归 0）
        total: 累计检测到的目标数
    产出：EventFact("mock","consecutive",n, meta={"brightness":...})。
    """

    def __init__(self, name: str = "mock"):
        super().__init__(name=name)
        self._sm = {
            "last_ts": 0.0,
            "consecutive": 0,
            "total": 0,
        }

    def trans(self, frames: List[DetectionOutput]) -> List[DetectionOutput]:
        return frames

    def infer(self, feats: List[DetectionOutput]) -> Dict[str, Any]:
        # 游标推进：仅处理上次 tick 之后的新帧。
        # slide_window 是非破坏性快照，连续 tick 大量重叠；
        # 若每 tick 重扫整窗会把同一帧重复计数，故用 last_ts 跳过已处理帧。
        last_ts = self._sm["last_ts"]
        new_frames = [f for f in feats if f.timestamp > last_ts]
        for output in new_frames:
            if len(output.detections) > 0:
                self._sm["consecutive"] += 1
                self._sm["total"] += len(output.detections)
            else:
                self._sm["consecutive"] = 0
        if new_frames:
            self._sm["last_ts"] = new_frames[-1].timestamp

        return {
            "consecutive": self._sm["consecutive"],
            "brightness": feats[-1].metadata.get("mean_brightness"),
        }

    def post_process(self, raw: Dict[str, Any], ts: float, online: bool) -> List[EventFact]:
        if not online:
            raise NotImplementedError("mock 离线分段产出待 Phase 2 实现")
        return [EventFact(
            source=self.name,
            signal="consecutive",
            value=raw["consecutive"],
            ts=ts,
            meta={"brightness": raw["brightness"]},
        )]


# ====== 时序线程 L4：Judge（消费事实出告警）======

class MockJudge(Judge):
    """Mock 判定（L4）。持 consecutive_trigger，连续命中达阈值上升沿触发。

    决策状态机（self._sm）：
        alarming: 上升沿锁存
        alarm_count: 累计告警次数
    """

    def __init__(self, consecutive_trigger: int = 3, name: str = "mock"):
        super().__init__(name=name)
        self.consecutive_trigger = consecutive_trigger
        self._sm = {"alarming": False, "alarm_count": 0}

    def step(self, facts: List[EventFact]) -> Tuple[List[str], List[AlarmInfo]]:
        if not facts:
            return [], []
        frame = self._frame(facts)
        f = frame.get("consecutive")
        if f is None:
            return [], []
        consecutive = f.value

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
                    "brightness": f.meta.get("brightness"),
                },
            ))
        elif not is_triggered and self._sm["alarming"]:
            self._sm["alarming"] = False

        return events, alarms
