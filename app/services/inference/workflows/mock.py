"""Mock 检测：MockDetector（流源）+ MockOperator（流算子，analyze+judge 合一）

用于无真实模型权重的 CPU 服务器验证推理链路，亦作未知 step 的 MOCK 透传 fallback。

MockDetector（推理线程）：
    纯 numpy 亮度启发式检测，无 YOLO 依赖。无状态，多 Client 共享，产出 "mock" 流。

MockOperator（时序线程，流算子）：
    订阅 "mock" 流。analyze：统计连续命中帧数 consecutive 入 _sm。
    judge：consecutive >= trigger → 上升沿锁存告警（trigger 设大值即纯透传）。
    有状态，每 Client 独立。
"""

from __future__ import annotations

import logging
from typing import List, Tuple

import numpy as np

from app.services.inference.detection.detector import Detector
from app.services.inference.temporal.operator import Operator
from app.domain.alarm import Alarm, AlarmType
from app.domain.detection import Detection, FrameDetections, FrameFeature
from app.domain.render import RenderItem, RenderSpec, RenderType

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


# ====== 时序线程：Operator（analyze 推进状态 + judge 出告警，共享 _sm）======

class MockOperator(Operator):
    """Mock 流算子。订阅 "mock" 流。持 consecutive_trigger，上升沿锁存告警。

    共享状态机（self._sm）：
        last_ts: 游标，已处理到的最新帧 timestamp（跨 tick 跳过重复帧）
        consecutive: 连续命中帧计数（命中 +1，未命中归 0）
        total: 累计检测到的目标数
        brightness: analyze 写、judge 读（最近一帧亮度，供告警元数据）
        alarming: judge 上升沿锁存
        alarm_count: 累计告警次数
    """

    def __init__(
        self,
        name: str = "mock",
        subscribes: List[str] = None,
        window_seconds: float = 10.0,
        consecutive_trigger: int = 3,
    ):
        super().__init__(
            name=name,
            subscribes=subscribes or ["mock"],
            window_seconds=window_seconds,
        )
        self.consecutive_trigger = consecutive_trigger
        self._sm = {
            "last_ts": 0.0,
            "consecutive": 0,
            "total": 0,
            "brightness": None,
            "alarming": False,
            "alarm_count": 0,
        }

    def analyze(self, windows: List[FrameFeature]) -> None:
        window = self.primary_window(windows)
        if not window:
            return
        # 游标推进：仅处理上次 tick 之后的新帧（slide_window 是非破坏性快照，
        # 连续 tick 大量重叠；若每 tick 重扫整窗会把同一帧重复计数）。
        last_ts = self._sm["last_ts"]
        new_frames = [f for f in window if f.timestamp > last_ts]
        for output in new_frames:
            if len(output.detections) > 0:
                self._sm["consecutive"] += 1
                self._sm["total"] += len(output.detections)
            else:
                self._sm["consecutive"] = 0
        if new_frames:
            self._sm["last_ts"] = new_frames[-1].timestamp
        self._sm["brightness"] = window[-1].metadata.get("mean_brightness")

    def judge(self) -> Tuple[List[str], List[Alarm]]:
        consecutive = self._sm["consecutive"]
        is_triggered = consecutive >= self.consecutive_trigger
        events = (
            [f"mock_object detected in {consecutive} consecutive frames"]
            if is_triggered else []
        )
        alarms: List[Alarm] = []

        if is_triggered and not self._sm["alarming"]:
            self._sm["alarming"] = True
            self._sm["alarm_count"] += 1
            alarms.append(Alarm(
                alarm_type=AlarmType.MOCK,
                alarm_level="low",
                alarm_message=f"Mock detection triggered ({consecutive} consecutive frames)",
                metadata={
                    "consecutive_frames": consecutive,
                    "brightness": self._sm["brightness"],
                },
            ))
        elif not is_triggered and self._sm["alarming"]:
            self._sm["alarming"] = False

        return events, alarms
