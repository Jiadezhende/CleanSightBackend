"""离线段 (offline) —— run 结束后对整段检测序列做的两件事，两个功能模块互不 import：

    动作分割   segmenter / runner / materials / impl/   detections.jsonl → OfflineSegmenter → temporal.jsonl + label_probs.npz
    规则判定   compliance/                              label_probs.npz + detections.jsonl → 规则表 → alarms.jsonl

    共用层     service / instance（离线作业服务）、cli（子进程入口：run = 分割后接判定，check = 只重跑判定）

调用方显式给出 `(task_id, step_id)`，离线链路只识别这对稳定存储键；不接 client/CQ/在线 Operator/alarm 服务。
独立进程跑（见 cli.py）。分割策略实现全部收在 `offline/impl/`。

纯包标记，不做 re-export——消费方按需走深路径导入（`.runner` / `.segmenter` / `.compliance.checker`）。
"""
