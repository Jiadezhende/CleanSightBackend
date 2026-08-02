"""气泡漏气时序算子：BubbleOperator（Operator 子类）。

订阅 "bubble" 流。analyze：ByteTrack 跨帧追踪，统计新气泡出生率
birth_rate = sum(新气泡数) / 窗口帧数，写入共享 _sm。
judge：birth_rate > threshold → 漏气报警（上升沿触发锁存）。
有状态（tracker/seen_ids/last_ts/new_count_history/birth_rate/alarming），每 Client 独立。
同业务点的流源 Detector 见 detection/impl/bubble.py。
"""

import logging
from types import SimpleNamespace
from typing import List, Tuple

import numpy as np

from app.services.inference.temporal.operator import Operator
from app.domain.alarm import Alarm, AlarmMetric, AlarmType
from app.domain.detection import FrameDetections, FrameFeature

logger = logging.getLogger(__name__)

_BYTETRACK_ARGS = SimpleNamespace(
    track_high_thresh=0.25,
    track_low_thresh=0.1,
    new_track_thresh=0.25,
    track_buffer=30,
    match_thresh=0.8,
    fuse_score=True,
)


class _BBoxAdapter:
    """将 List[Detection] 适配为 BYTETracker.update() 所需的接口对象。"""

    def __init__(self, detections):
        if detections:
            rows, confs, cls_ids = [], [], []
            for d in detections:
                x1, y1, x2, y2 = d.bbox
                cx = (x1 + x2) / 2.0
                cy = (y1 + y2) / 2.0
                rows.append([cx, cy, float(x2 - x1), float(y2 - y1)])
                confs.append(d.confidence)
                cls_ids.append(float(d.class_id))
            self.xywh = np.array(rows, dtype=np.float32)
            self.conf = np.array(confs, dtype=np.float32)
            self.cls = np.array(cls_ids, dtype=np.float32)
        else:
            self.xywh = np.empty((0, 4), dtype=np.float32)
            self.conf = np.empty((0,), dtype=np.float32)
            self.cls = np.empty((0,), dtype=np.float32)

    def __len__(self):
        return len(self.conf)

    def __getitem__(self, idx):
        r = _BBoxAdapter.__new__(_BBoxAdapter)
        r.xywh = self.xywh[idx]
        r.conf = self.conf[idx]
        r.cls = self.cls[idx]
        return r


class BubbleOperator(Operator):
    """气泡漏气流算子。订阅 "bubble" 流，analyze 算 birth_rate、judge 上升沿告警。

    共享状态机（self._sm）：
        tracker: BYTETracker 实例（惰性初始化）
        seen_ids: 已确认过的 track_id 集合（永不清空）
        last_ts: 游标，已处理到的最新帧 timestamp
        new_count_history: [(timestamp, new_bubble_count), ...] 感受野内
        birth_rate: analyze 写、judge 读（最近一次出生率）
        alarming: judge 上升沿锁存
    """

    def __init__(
        self,
        name: str = "bubble",
        subscribes: List[str] = None,
        window_seconds: float = 3.0,
        birth_rate_threshold: float = 0.5,
    ):
        super().__init__(
            name=name,
            subscribes=subscribes or ["bubble"],
            window_seconds=window_seconds,
        )
        self.birth_rate_threshold = birth_rate_threshold
        self._sm = {
            "tracker": None,
            "seen_ids": set(),
            "last_ts": 0.0,
            "new_count_history": [],
            "birth_rate": 0.0,
            "alarming": False,
        }

    def analyze(self, windows: List[FrameFeature]) -> None:
        window = self.primary_window(windows)
        if not window:
            return
        self._advance(window)
        self._sm["birth_rate"] = self._compute_metric()

    def judge(self) -> Tuple[List[str], List[Alarm]]:
        birth_rate = self._sm["birth_rate"]
        is_triggered = birth_rate > self.birth_rate_threshold
        events = (
            [f"bubble_birth_rate={birth_rate:.2f} (>{self.birth_rate_threshold})"]
            if is_triggered else []
        )
        alarms: List[Alarm] = []

        if is_triggered and not self._sm["alarming"]:
            self._sm["alarming"] = True
            alarms.append(Alarm(
                alarm_type=AlarmType.PROCESS_VIOLATION,
                alarm_level="high",
                alarm_message=f"持续产生新气泡（birth_rate={birth_rate:.2f}），疑似漏气",
                metric=AlarmMetric.BUBBLE,
                metadata={"birth_rate": birth_rate, "threshold": self.birth_rate_threshold},
            ))
        elif not is_triggered and self._sm["alarming"]:
            self._sm["alarming"] = False

        return events, alarms

    def _advance(self, window: List[FrameDetections]) -> None:
        """游标推进：仅处理上次 tick 之后的新帧，驱动 ByteTrack 并记录 new_count。"""
        last_ts = self._sm["last_ts"]
        new_frames = [f for f in window if f.timestamp > last_ts]
        if not new_frames:
            return

        if self._sm["tracker"] is None:
            from ultralytics.trackers.byte_tracker import BYTETracker
            self._sm["tracker"] = BYTETracker(_BYTETRACK_ARGS, frame_rate=10)

        cutoff = window[-1].timestamp - self.window_seconds

        for frame in new_frames:
            adapter = _BBoxAdapter(frame.detections)
            out = self._sm["tracker"].update(adapter)

            if len(out) > 0:
                current_ids = set(out[:, 4].astype(int))
                new_count = len(current_ids - self._sm["seen_ids"])
                self._sm["seen_ids"].update(current_ids)
            else:
                new_count = 0

            self._sm["new_count_history"].append((frame.timestamp, new_count))

        self._sm["new_count_history"] = [
            (ts, n) for ts, n in self._sm["new_count_history"] if ts >= cutoff
        ]
        self._sm["last_ts"] = new_frames[-1].timestamp

    def _compute_metric(self) -> float:
        history = self._sm["new_count_history"]
        if not history:
            return 0.0
        return sum(n for _, n in history) / len(history)
