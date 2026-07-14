"""离线段 (offline)。

本层为离线动作分割流水线预留：调用方显式给出 (task_id, step_id)，从 FeatureStore
读取完整检测序列，经 stage 配置实例化 OfflineSegmenter 产出 SegmentFact 幂等写入 FactLedger。

当前一期先落地最小闭环：
    features.jsonl -> OfflineTemporalModel -> timeline -> SegmentFact / FactLedger。

离线链路只识别稳定存储键 (task_id, step_id)，不接 client/CQ/在线 Operator/告警。
"""

from typing import Any

__all__: list[str] = [
    "BrushRuleSegmenter",
    "FeatureVectorizer",
    "HeuristicBrushSegmenter",
    "ModelInput",
    "OfflineFeatureSequence",
    "OfflineInferenceResult",
    "OfflineTemporalModel",
    "TimelineSegment",
    "OfflineInferenceWorker",
    "SEGMENTER_REGISTRY",
    "create_segmenter",
    "query_timeline",
]


def __getattr__(name: str) -> Any:
    """懒加载导出对象，避免 `python -m app.services.inference.offline.worker` 重复导入 warning。"""
    if name in {"BrushRuleSegmenter", "FeatureVectorizer", "ModelInput", "SEGMENTER_REGISTRY", "create_segmenter"}:
        from app.services.inference.offline import segmenter

        return getattr(segmenter, name)
    if name == "HeuristicBrushSegmenter":
        from app.services.inference.offline.heuristic import HeuristicBrushSegmenter

        return HeuristicBrushSegmenter
    if name in {"OfflineFeatureSequence", "OfflineInferenceResult", "OfflineTemporalModel", "TimelineSegment"}:
        from app.services.inference.offline import interfaces

        return getattr(interfaces, name)
    if name in {"OfflineInferenceWorker", "query_timeline"}:
        from app.services.inference.offline import worker

        return getattr(worker, name)
    raise AttributeError(name)
