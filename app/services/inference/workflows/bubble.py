"""气泡检测：BubbleDetector + BubbleAnalyzer（L3 产事实）+ BubbleJudge（L4 出告警）

BubbleDetector（推理线程）：
    YOLO11n-seg 检测气泡实例，输出 DetectionOutput。
    无状态，多 Client 共享同一实例。

BubbleAnalyzer（时序线程，L3）：
    ByteTrack 跨帧追踪，统计新气泡出生率（birth_rate）。
    birth_rate = sum(新气泡数) / 窗口帧数。只产 EventFact("bubble","birth_rate",rate)，不判定。
    有状态（tracker/seen_ids/last_ts/new_count_history），每个 Client 独立实例化。

BubbleJudge（时序线程，L4）：
    持 birth_rate 阈值，birth_rate > threshold → 漏气报警（上升沿触发锁存）。
"""

import logging
import time
from types import SimpleNamespace
from typing import Any, Dict, List, Tuple

import numpy as np

from app.services.inference.workflows.detector import YOLODetector
from app.services.inference.workflows.analyzer import TemporalAnalyzer
from app.services.inference.workflows.judge import Judge
from app.services.inference.data_models import (
    AlarmInfo,
    AlarmType,
    DetectionOutput,
    EventFact,
    VisualizationData,
    VisItem,
    VisualizationType,
)

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


# ====== 推理线程：Detector ======

class BubbleDetector(YOLODetector):
    """气泡检测器（YOLO11n-seg）。无状态，多 Client 共享。"""

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

    def infer_batch(
        self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
    ) -> List[DetectionOutput]:
        try:
            outputs = self._run_yolo_batch(frames)
            for output in outputs:
                output.success = True
                output.bubble_detected = len(output.detections) > 0
                output.bubble_count = len(output.detections)
            return outputs
        except Exception as e:
            logger.error("[BubbleDetector] Batch inference failed, fallback: %s", e, exc_info=True)
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

    def prepare_visualization_data(self, output: DetectionOutput) -> VisualizationData:
        items = []
        for det in output.detections:
            color = (255, 0, 255) if det.class_name == "bubble_debug_box" else (0, 255, 255)
            items.append(VisItem(
                bbox=det.bbox,
                label=f"{det.class_name} {det.confidence:.2f}",
                confidence=det.confidence,
                color=color,
            ))

        count = len(output.detections)
        if count > 0:
            status_text = f"Bubbles: {count}"
            status_color = (0, 165, 255) if count > 5 else (0, 255, 255)
        else:
            status_text = "No Bubbles"
            status_color = (0, 255, 0)

        return VisualizationData(
            type=VisualizationType.BBOX,
            items=items,
            status_text=status_text,
            status_color=status_color,
            status_position="top-right",
        )


# ====== 时序线程 L3：TemporalAnalyzer（只产事实）======

class BubbleAnalyzer(TemporalAnalyzer):
    """气泡出生率时序分析器（L3）。有状态，每个 Client 独立实例化。

    测量状态机（self._sm）：
        tracker: BYTETracker 实例（惰性初始化）
        seen_ids: 已确认过的 track_id 集合（永不清空）
        last_ts: 游标，已处理到的最新帧 timestamp
        new_count_history: [(timestamp, new_bubble_count), ...] 滑动窗口
    产出：EventFact("bubble", "birth_rate", rate)，裸值不带阈值（阈值归 BubbleJudge）。
    """

    def __init__(self, window_seconds: float = 3.0, name: str = "bubble"):
        super().__init__(name=name)
        self.window_seconds = window_seconds
        self._sm = {
            "tracker": None,
            "seen_ids": set(),
            "last_ts": 0.0,
            "new_count_history": [],
        }

    def trans(self, frames: List[DetectionOutput]) -> List[DetectionOutput]:
        # 上游聚合特征即逐帧 bbox，时序模型直接消费，无需变换。
        return frames

    def infer(self, feats: List[DetectionOutput]) -> float:
        self._advance(feats)
        return self._compute_metric()

    def post_process(self, raw: float, ts: float, online: bool) -> List[EventFact]:
        if not online:
            raise NotImplementedError("bubble 离线分段产出待 Phase 2 实现")
        return [EventFact(source=self.name, signal="birth_rate", value=raw, ts=ts)]

    def _advance(self, window: List[DetectionOutput]) -> None:
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


# ====== 时序线程 L4：Judge（消费事实出告警）======

class BubbleJudge(Judge):
    """气泡漏气判定（L4）。持 birth_rate 阈值，上升沿触发锁存。"""

    def __init__(self, birth_rate_threshold: float = 0.5, name: str = "bubble"):
        super().__init__(name=name)
        self.birth_rate_threshold = birth_rate_threshold
        self._sm = {"alarming": False}

    def step(self, facts: List[EventFact]) -> Tuple[List[str], List[AlarmInfo]]:
        if not facts:
            return [], []
        frame = self._frame(facts)
        f = frame.get("birth_rate")
        if f is None:
            return [], []
        birth_rate = f.value

        is_triggered = birth_rate > self.birth_rate_threshold
        events = (
            [f"bubble_birth_rate={birth_rate:.2f} (>{self.birth_rate_threshold})"]
            if is_triggered else []
        )
        alarms: List[AlarmInfo] = []

        if is_triggered and not self._sm["alarming"]:
            self._sm["alarming"] = True
            alarms.append(AlarmInfo(
                alarm_type=AlarmType.PROCESS_VIOLATION,
                alarm_level="high",
                alarm_message=f"持续产生新气泡（birth_rate={birth_rate:.2f}），疑似漏气",
                metadata={"birth_rate": birth_rate, "threshold": self.birth_rate_threshold},
            ))
        elif not is_triggered and self._sm["alarming"]:
            self._sm["alarming"] = False

        return events, alarms
