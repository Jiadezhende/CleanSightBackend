"""Mock 时序算子：MockOperator（Operator 子类）。

订阅 "mock" 流。analyze：统计连续命中帧数 consecutive 入 _sm。
judge：consecutive >= trigger → 上升沿锁存告警（trigger 设大值即纯透传）。
有状态，每 Client 独立。同业务点的流源 Detector 见 detection/impl/mock.py。
"""

from __future__ import annotations

from typing import List, Tuple

from app.services.inference.temporal.operator import Operator
from app.domain.alarm import Alarm, AlarmType
from app.domain.detection import FrameFeature


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
