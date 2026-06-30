"""feature_store 层 (L2)。

FeatureStore：online 推理写回常开落盘 features.jsonl（按帧 ts 对齐）。
FactLedger：offline 预留事实账本 facts.jsonl（在线链路不产，离线 segmenter 写 SegmentFact）。
"""

from .store import FactLedger, FeatureStore

__all__ = [
    "FeatureStore",
    "FactLedger",
]
