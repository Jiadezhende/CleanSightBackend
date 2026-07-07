"""离线分割策略基类 —— 两段接缝：输入预处理层（预留）+ 模型推理分割。

一个离线推理策略 = 一个 `OfflineSegmenter` 子类（对齐在线 Detector/Operator「加一子类」范式，
见 docs/kb/DESIGN_EXTENDING_DETECTION.md）。策略实现全部收在 `offline/segmenters/`，框架层
（base/runner/cli）不掺实现。

管线两段：
    load_many(subscribes) → raw FrameDetections 序列（按 source 分组、按 ts 升序）
        │
        ▼ preprocess(streams)   ← 输入预处理层（预留）：raw bbox 序列不一定能直接喂模型，
        │                          需张量化/归一化/时间降采样/定长编码的模型在此转换
        ▼ segment(model_input)  ← 模型推理 + 解码为 SegmentFact
        │
        ▼ List[SegmentFact]

约束：
- `streams` 只读，不得原地修改；
- 策略不访问 FeatureStore / FactLedger / ClientManager / CQ / 数据库（纯算法）；
- 输出每条 `SegmentFact.source` 必须等于本策略 `name`；`start <= end`、时间为有限数、`0 <= conf <= 1`
  （由 Runner 统一校验，见 runner.py）。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Mapping, Sequence

from app.domain.detection import FrameDetections
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

    def preprocess(
        self, streams: Mapping[str, Sequence[FrameDetections]]
    ) -> Any:
        """输入预处理层（预留接缝）：把 raw FrameDetections 序列转成模型可消费的输入。

        默认恒等透传 `streams`——适合直接吃 bbox 序列的轻量/占位策略。需要张量化、归一化、
        时间降采样、定长编码的重模型 override 本方法（或委托同模块内的 adapter 类，保持内聚）。
        返回类型由策略自定，原样传给 `segment`。
        """
        return streams

    @abstractmethod
    def segment(self, model_input: Any) -> List[SegmentFact]:
        """消费 `preprocess` 的输出，做模型推理并解码为动作分段事实。"""
        raise NotImplementedError
