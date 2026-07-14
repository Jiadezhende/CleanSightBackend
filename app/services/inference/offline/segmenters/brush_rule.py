"""将本地规则式离线时序模型接到远端 OfflineSegmenter 入口。"""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence

from app.domain.detection import Detection, FrameDetections
from app.services.inference.models import SegmentFact
from app.services.inference.offline.base import OfflineSegmenter
from app.services.inference.offline.interfaces import OfflineFeatureSequence, OfflineFrame
from app.services.inference.offline.segmenter.brush_rule import BrushRuleSegmenter as RuleModel


class BrushRuleSegmenter(OfflineSegmenter):
    """基于本地规则时序模型的离线分段策略。"""

    def __init__(
        self,
        name: str,
        subscribes: Sequence[str],
        label: str = "brush_rule",
        min_duration_s: float = 0.2,
    ):
        super().__init__(name, subscribes)
        self.label = label
        self.min_duration_s = max(0.0, float(min_duration_s))
        self.model = RuleModel()

    def preprocess(self, streams: Mapping[str, Sequence[FrameDetections]]) -> OfflineFeatureSequence:
        """把远端 runner 的 FrameDetections 序列整理为本地 OfflineFeatureSequence。"""
        ts_to_sources: dict[float, dict[str, list[Detection]]] = {}
        for source in self.subscribes:
            for frame in streams.get(source, ()):
                bucket = ts_to_sources.setdefault(float(frame.timestamp), {})
                bucket[source] = list(frame.detections)

        frames = [
            OfflineFrame(
                timestamp=ts,
                detections_by_source={src: dets for src, dets in sorted(bucket.items())},
            )
            for ts, bucket in sorted(ts_to_sources.items())
        ]

        return OfflineFeatureSequence(
            task_id=0,
            step_id=0,
            frames=frames,
            sources=list(self.subscribes),
            fps=7.5,
            meta={"source": self.name},
        )

    def segment(self, model_input: Any) -> List[SegmentFact]:
        sequence: OfflineFeatureSequence = model_input
        predictions = self.model.predict(sequence)
        timeline: List[dict[str, Any]] = []
        current_label: str | None = None
        current_start: float | None = None
        current_end: float | None = None
        current_conf = 0.0
        current_count = 0

        for pred in predictions:
            if pred.label == "idle":
                if current_label is not None and current_end is not None and current_start is not None:
                    duration = current_end - current_start
                    if duration >= self.min_duration_s:
                        timeline.append({
                            "label": current_label,
                            "start": current_start,
                            "end": current_end,
                            "conf": current_conf / max(current_count, 1),
                        })
                current_label = None
                current_start = None
                current_end = None
                current_conf = 0.0
                current_count = 0
                continue

            if current_label != pred.label:
                if current_label is not None and current_start is not None and current_end is not None:
                    duration = current_end - current_start
                    if duration >= self.min_duration_s:
                        timeline.append({
                            "label": current_label,
                            "start": current_start,
                            "end": current_end,
                            "conf": current_conf / max(current_count, 1),
                        })
                current_label = pred.label
                current_start = pred.timestamp
                current_end = pred.timestamp
                current_conf = pred.confidence
                current_count = 1
            else:
                current_end = pred.timestamp
                current_conf += pred.confidence
                current_count += 1

        if current_label is not None and current_start is not None and current_end is not None:
            duration = current_end - current_start
            if duration >= self.min_duration_s:
                timeline.append({
                    "label": current_label,
                    "start": current_start,
                    "end": current_end,
                    "conf": current_conf / max(current_count, 1),
                })

        return [
            SegmentFact(
                source=self.name,
                label=item["label"] if item["label"] != "idle" else self.label,
                start=item["start"],
                end=item["end"],
                conf=min(1.0, max(0.0, item["conf"])),
            )
            for item in timeline
        ]
