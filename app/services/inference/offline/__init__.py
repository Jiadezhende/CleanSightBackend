"""离线段 (offline) —— 预留层占位。

本层为离线动作分割流水线预留：调用方显式给出 (task_id, step_id)，从 FeatureStore
读取完整检测序列，经 stage 配置实例化 OfflineSegmenter 产出 SegmentFact 幂等写入 FactLedger。

当前为空占位。一期实现（OfflineSegmenter / OfflineRunner / CLI / StageFactory.create_offline_segmenter
/ FeatureStore.load_many / FactLedger.replace_segments）是独立任务，见
docs/update/20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md。

离线链路只识别稳定存储键 (task_id, step_id)，不接 client/CQ/在线 Operator/告警。
"""

__all__: list[str] = []
