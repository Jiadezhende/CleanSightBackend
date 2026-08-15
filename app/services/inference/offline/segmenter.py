"""离线分割策略基类 —— 一个抽象方法，仅此而已。

一个离线推理策略 = 一个 `OfflineSegmenter` 子类（对齐在线 Detector/Operator「加一子类」
范式，见 docs/kb/DESIGN_EXTENDING_DETECTION.md）。

    runner 给 (task_id, step_id)
        │
        ▼ segment(task_id, step_id)
        │     └─ 策略自己按需 blocks.load(...) 取特征块、拼输入、前向、解码
        ▼
    List[SegmentFact]  → 由 runner 校验并幂等写入 FactLedger

**基类刻意不设 `preprocess` 这层中转。** 早先的 `preprocess(frames) -> model_input`
把「特征怎么来」焊死成了「从 frames 算」，签名里没有视觉这一路，融合 Segmenter 因此
根本写不出来。现在特征从哪来是策略自己的事（`blocks.load` 想取几块取几块），基类不掺和。

约束：
- 策略不访问 FeatureStore / FactLedger / ClientManager / CQ / 数据库——特征只经 `blocks`
  这一个口子进来（`blocks` 内部才碰 FeatureStore）；
- 输出每条 `SegmentFact.source` 必须等于本策略 `name`；`start <= end`、时间为有限数、
  `0 <= conf <= 1`（由 runner 统一校验）；
- 输入无特征时 `blocks.load` 抛 `NoFeatures`，策略不必自己判空。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

from app.services.inference.models import SegmentFact


class OfflineSegmenter(ABC):
    """离线全序列分割策略基类。"""

    def __init__(self, name: str, subscribes: Sequence[str]):
        if not name:
            raise ValueError("offline segmenter name is required")
        if not subscribes:
            raise ValueError("offline segmenter subscribes is required")
        self.name = name
        self.subscribes: List[str] = list(subscribes)

    @abstractmethod
    def segment(self, task_id: int, step_id: int) -> List[SegmentFact]:
        """跑完一条 step，产出动作分段事实。"""
        raise NotImplementedError

    def debug_result(self) -> Optional[dict]:
        """可选：返回上一次 `segment()` 的逐帧调试产物（纯 dict），默认无。

        产逐帧预测的策略可 override。runner 若拿到非 None，会落一份到 `.cache`——它是
        **可重建的调试产物**，不是正式结果（正式结果是 FactLedger 里的 SegmentFact）。
        """
        return None
