"""Mock 离线分割策略：presence 型动作分段（离线链路的无权重 CPU 验证 stand-in）。

对齐 workflows/mock.py 的 MockDetector 定位：不接真实模型、纯启发式，用最少假设让整条离线
入口端到端跑通。算法：把订阅的多路序列按 ts 归并，某 ts「有动作」= 任一订阅 source 在该 ts
至少有一个检测框；相邻「有动作」帧归并成一段，产一条 SegmentFact。与检测类别无关。

接进 MOCK stage 的 `offline` 配置（见 config/inference_config.yaml），用未配置的数字 step_id
（如 -1）经 resolve_stage 回退到 MOCK 手动跑，验证 Store→路由→策略→Ledger 全链路。

不访问 FeatureStore / FactLedger / ClientManager / DB（纯算法）。
"""

from __future__ import annotations

from typing import Any, List, Mapping, Sequence

from app.domain.detection import FrameDetections
from app.services.inference.models import SegmentFact
from app.services.inference.offline.segmenter import OfflineSegmenter


class MockSegmenter(OfflineSegmenter):
    """presence 型占位分段器（MOCK 链路 stand-in）。

    Args:
        name: 策略身份（= SegmentFact.source），工厂注入。
        subscribes: 订阅的 detector name 列表，工厂注入。
        label: 产出分段的动作标签（默认 "active"）。
        min_frames: 一段至少包含的「有动作」帧数，低于此丢弃（默认 1）。
    """

    def __init__(
        self,
        name: str,
        subscribes: Sequence[str],
        label: str = "active",
        min_frames: int = 1,
    ):
        super().__init__(name, subscribes)
        self.label = label
        self.min_frames = max(1, int(min_frames))

    def segment(self, model_input: Any) -> List[SegmentFact]:
        streams: Mapping[str, Sequence[FrameDetections]] = model_input
        # 归并多路时间线：ts -> 是否有动作（任一 source 有检测）
        active_by_ts: dict[float, bool] = {}
        for src in self.subscribes:
            for fd in streams.get(src, ()):
                has = bool(fd.detections)
                active_by_ts[fd.timestamp] = active_by_ts.get(fd.timestamp, False) or has

        segments: List[SegmentFact] = []
        run_start: float | None = None
        run_last: float = 0.0
        run_count = 0
        for ts in sorted(active_by_ts):
            if active_by_ts[ts]:
                if run_start is None:
                    run_start = ts
                    run_count = 0
                run_last = ts
                run_count += 1
            else:
                if run_start is not None and run_count >= self.min_frames:
                    segments.append(self._make(run_start, run_last))
                run_start = None
        if run_start is not None and run_count >= self.min_frames:
            segments.append(self._make(run_start, run_last))
        return segments

    def _make(self, start: float, end: float) -> SegmentFact:
        return SegmentFact(source=self.name, label=self.label, start=start, end=end, conf=1.0)
