"""测试夹具：不依赖 torch 的规则 Segmenter。

**这是脚手架，不是兜底。** 它曾挂在 `config/inference_config.yaml` 的 MOCK stage 上、
被文档写成「离线的真兜底」，但与代码不符——离线只由手动 CLI 触发，在线链路对它零引用，
没有会崩的在线路径需要它兜；配错 class 路径时工厂是 fail-fast 抛错，也根本不退到 mock。

保留它的真实价值只有一个：**不装 torch 也能验证 `blocks → Segmenter → FactLedger` 回环**。
那是测试关心的事，所以它住在 tests/ 下，由用例自己在临时配置里挂载。

放 tests/ 根目录而非 factories.py：factories.py 是**数据构造**的单一真源，这是**行为
stub**，两者不是一回事。
"""

from __future__ import annotations

from typing import List, Sequence

from app.domain.detection import FrameFeature
from app.services.inference.models import SegmentFact
from app.services.inference.offline.blocks import BlockKind
from app.services.inference.offline.segmenter import OfflineSegmenter


class BrushRulesSegmenter(OfflineSegmenter):
    """纯规则 Mock 分段器：任一订阅 source 在该帧有检测框即 active，连续 active 帧合成一段。

    Args:
        name: 策略身份，必须等于产出 SegmentFact.source。
        subscribes: 订阅的 detector/source 名称列表，由 StageFactory 从 YAML 注入。
        label: active 片段写出的动作标签，默认 `mock_action`。
        min_frames: 一个片段至少包含多少个 active 采样帧。
    """

    needs = (BlockKind.BBOX,)
    backbone = None

    def __init__(
        self,
        name: str,
        subscribes: Sequence[str],
        label: str = "mock_action",
        min_frames: int = 1,
        storage_dir=None,
        offline_dir=None,
    ):
        super().__init__(name, subscribes)
        self.label = label
        self.min_frames = max(1, int(min_frames))
        self.storage_dir = storage_dir
        self.offline_dir = offline_dir

    def build_input(self, blocks):
        """规则实现不吃拼装后的矩阵，但仍实现本方法，让 `export` 子命令对它也可用。"""
        return blocks[BlockKind.BBOX]

    def segment(self, task_id: int, step_id: int) -> List[SegmentFact]:
        return self._segment_frames(self._load_frames(task_id, step_id))

    def _load_frames(self, task_id: int, step_id: int) -> Sequence[FrameFeature]:
        """规则逻辑要看原始检测框而非 71 维特征，故直接走 blocks 的取帧口。"""
        from app.services.inference.offline import blocks as blocks_api

        return blocks_api.load_frames(task_id, step_id, self.subscribes, self.storage_dir)

    def _segment_frames(self, frames: Sequence[FrameFeature]) -> List[SegmentFact]:
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
