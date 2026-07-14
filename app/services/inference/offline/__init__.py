"""离线段 (offline) —— 全序列动作分割入口。

调用方显式给出 `(task_id, step_id)`，从 FeatureStore 读取完整检测序列（`FrameDetections`），
经 stage 配置实例化 `OfflineSegmenter` 产出 `SegmentFact`，幂等写入 FactLedger。

离线链路只识别稳定存储键 `(task_id, step_id)`，不接 client/CQ/在线 Operator/告警；
独立进程手动跑（见 cli.py）。策略实现全部收在 `offline/segmenters/`。
"""

from app.services.inference.offline.runner import OfflineRunner, OfflineRunResult, OfflineRunSpec
from app.services.inference.offline.segmenter import OfflineSegmenter

__all__: list[str] = [
    "OfflineRunner",
    "OfflineRunResult",
    "OfflineRunSpec",
    "OfflineSegmenter",
]
