"""离线链路 Mock/兜底策略：brush_rules。

作用:
    这是离线链路的轻量兜底实现，不代表真实模型。它用于两类场景：
    1. YAML 配置非法或真实模型权重暂不可用时，仍可用最低成本验证
       FeatureStore -> OfflineRunner -> SegmentFact -> FactLedger 的回环；
    2. 单测/本地 smoke test 不依赖 torch、GPU、真实 clean 权重。

输入:
    preprocess(frames) 接收 FeatureStore.load 返回的 List[FrameFeature]，原样传给 segment。

输出:
    segment(model_input) 返回 List[SegmentFact]。只要某帧任一订阅 source 存在检测框，
    就认为该帧 active；连续 active 帧合并为一个片段。
"""

from __future__ import annotations

from typing import Any, List, Sequence

from app.domain.detection import FrameFeature
from app.services.inference.types import SegmentFact
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

    def preprocess(self, frames: Sequence[FrameFeature]) -> Sequence[FrameFeature]:
        """Mock 不做特征工程，直接把帧序列交给规则逻辑。"""
        return frames

    def segment(self, model_input: Any) -> List[SegmentFact]:
        frames: Sequence[FrameFeature] = model_input
        segments: List[SegmentFact] = []
        run_start: float | None = None
        run_last = 0.0
        run_count = 0
        for ff in frames:  # load 已按 ts 升序
            active = any(fd.detections for fd in ff.by_source.values())
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

    def _make(self, start: float, end: float) -> SegmentFact:
        return SegmentFact(
            source=self.name,
            label=self.label,
            start=float(start),
            end=float(end),
            conf=1.0,
            meta={"model_version": "brush_rules_v1"},
        )
