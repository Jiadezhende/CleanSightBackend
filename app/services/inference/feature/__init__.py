"""feature_store 层 (L2)。

FeatureStore：online 推理写回常开落盘 features.jsonl（按帧 ts 对齐），生命周期随在线 run（由 InferenceManager open_fresh/close/flush）。
FactLedger：offline 预留事实账本 facts.jsonl。**离线异步写**，生命周期归离线 runner——
    在线 manager 不持有、不调度；待离线流水线建起时由其自行 new + 驱动（同一 storage_base_dir）。休眠预留。
"""

from .store import FactLedger, FeatureStore

__all__ = [
    "FeatureStore",
    "FactLedger",
]
