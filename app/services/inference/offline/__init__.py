"""离线段 (offline) —— run 结束后对整段检测序列做的两件事，分两个子模块，互不 import：

    segmentation/   动作分割   detections.jsonl → OfflineSegmenter → temporal.jsonl + label_probs.npz
    compliance/     规则判定   label_probs.npz + detections.jsonl → 规则表 → alarms.jsonl

    service / instance   离线作业服务（排队、起子进程、状态）
    worker               编排分割、结果提交与规则判定
    analysis_child       子进程侧：运行环境初始化（钉卡/限线程/优先级）与 spawn 入口
    analysis_proxy       父进程侧：子进程句柄（等结果/kill/回收）与 spawn
    cli                  人工入口：参数解析、输出与退出码（不被包内模块 import）

调用方显式给出 `(task_id, step_id)`，离线链路只识别这对稳定存储键；不接 client/CQ/在线 Operator/alarm 服务。

纯包标记，消费方走深路径（`.worker` / `.compliance.checker`）。
"""
