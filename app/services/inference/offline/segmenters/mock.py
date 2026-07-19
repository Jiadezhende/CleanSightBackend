"""离线链路 Mock/兜底策略：brush_rules。

作用:
    这是离线链路的轻量兜底实现，不代表真实模型。它用于两类场景：
    1. YAML 配置非法或真实模型权重暂不可用时，仍可用最低成本验证
       FeatureStore -> OfflineRunner -> SegmentFact -> FactLedger 的回环；
    2. 单测/本地 smoke test 不依赖 torch、GPU、真实 clean 权重。

输入:
    preprocess(streams) 接收 FeatureStore.load_many 返回的
    Mapping[source, Sequence[FrameDetections]]，原样传给 segment。

输出:
    segment(model_input) 返回 List[SegmentFact]。只要某个 ts 的任一订阅 source
    存在检测框，就认为该帧 active；连续 active 帧合并为一个片段。
"""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence

from app.domain.detection import FrameDetections
from app.services.inference.models import SegmentFact
from app.services.inference.offline.segmenter import OfflineSegmenter


class BrushRulesSegmenter(OfflineSegmenter):
    """纯规则 Mock 分段器。

    Args:
        name: 策略身份，必须等于产出 SegmentFact.source。
        subscribes: 订阅的 detector/source 名称列表，由 StageFactory 从 YAML 注入。
        label: active 片段写出的动作标签，默认 `mock_action`。
        min_frames: 一个片段至少包含多少个 active 采样帧。
    """

    def __init__(
        self,
        name: str,
        subscribes: Sequence[str],
        label: str = "mock_action",
        min_frames: int = 1,
    ):
        super().__init__(name, subscribes)
        self.label = label
        self.min_frames = max(1, int(min_frames))

    def preprocess(
        self, streams: Mapping[str, Sequence[FrameDetections]]
    ) -> Mapping[str, Sequence[FrameDetections]]:
        """Mock 不做特征工程，直接把原始检测序列交给规则逻辑。"""
        return streams

    def segment(self, model_input: Any) -> List[SegmentFact]:
        streams: Mapping[str, Sequence[FrameDetections]] = model_input
        active_by_ts: dict[float, bool] = {}
        for src in self.subscribes:
            for fd in streams.get(src, ()):
                ts = float(fd.timestamp)
                active_by_ts[ts] = active_by_ts.get(ts, False) or bool(fd.detections)

        segments: List[SegmentFact] = []
        run_start: float | None = None
        run_last = 0.0
        run_count = 0
        for ts in sorted(active_by_ts):
            if active_by_ts[ts]:
                if run_start is None:
                    run_start = ts
                    run_count = 0
                run_last = ts
                run_count += 1
                continue

            if run_start is not None and run_count >= self.min_frames:
                segments.append(self._make(run_start, run_last))
            run_start = None
            run_count = 0

        if run_start is not None and run_count >= self.min_frames:
            segments.append(self._make(run_start, run_last))
        return segments

    def _make(self, start: float, end: float) -> SegmentFact:
        return SegmentFact(
            source=self.name,
            label=self.label,
            start=float(start),
            end=float(end),
            conf=1.0,
            meta={"model_version": "brush_rules_v1"},
        )
