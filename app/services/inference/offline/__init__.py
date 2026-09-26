"""离线段 (offline) —— 全序列动作分割。

调用方显式给出 `(task_id, step_id)`，从 `app.storage.inference` 读取完整检测序列
（`FrameDetection`），经 stage 配置实例化 `OfflineSegmenter` 产出 `TemporalSegment`，幂等写回 temporal.jsonl。

离线链路只识别稳定存储键 `(task_id, step_id)`，不接 client/CQ/在线 Operator/告警；
独立进程跑（见 cli.py）。策略实现全部收在 `offline/impl/`。

纯包标记，不做 re-export——消费方按需走深路径导入（`.runner` / `.segmenter`）。
"""
