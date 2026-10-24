# 帧窗迁成 `DETECTION_WINDOW` topic，`/task` 信号汇总迁到 router

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 4
> **知识库**：待沉淀

## 概述

CQ 的 `_slide_window`（帧窗）迁成 `inference/online/topics.py` 的 `DETECTION_WINDOW`（`TimeWindow`），CQ 删除 `set_stream_windows` / `push_detection` / `get_slide_window` / `get_slide_window_summary`。
`/task/message` 的 signals_10s 汇总挪进 `routers/task.py` 的 `_summarize_window`。接口与汇总口径不变。

## 变更背景

- **承接**：批 3（[20261024_SESSION_MIGRATE_VIZ_SLOTS](20261024_SESSION_MIGRATE_VIZ_SLOTS.md)）已把 viz 读的三个单槽迁成 topic。
- 帧窗是推理概念（感受野、时序算子），留在 client 包正是提案痛点 #1。

## 方案详情

### 全景

```text
写回 ── DETECTION_WINDOW.push(frame) ──▶ actor tick：snapshot → operator.analyze / judge
                                    └──▶ GET /task/message：snapshot → _summarize_window（最近 10s）→ metric 映射
start_workflow：实例化算子后 DETECTION_WINDOW.extend_span(max 感受野)
```

| 改动 | 落在哪 |
|------|--------|
| `SIGNALS_WINDOW_SEC = 10.0`、`DETECTION_WINDOW = TimeWindow(span=10s)` | `app/services/inference/online/topics.py` |
| 写回 push、actor snapshot、start_workflow 放宽保留时长 | `detection/service.py`、`temporal/actor.py`、`online/service.py` |
| `_summarize_window`（原 `get_slide_window_summary` 原样平移，入参改为帧窗快照） | `app/routers/task.py` |
| CQ 删帧窗字段与 4 个方法；`_slide_window_lock` 只剩护 `ca_detections` | `app/services/client/queues.py` |

- **保留时长口径不变**：旧 `set_stream_windows` = max(10s, 各订阅流的算子最大感受野)；每个算子至少订阅一条流，故等于 max(10s, 全部算子最大感受野)，即 `extend_span(max(op.window_seconds))` 叠加构造时的 10s 底线。
- **汇总在锁外算**：旧实现在 CQ 锁内遍历帧窗；现在先 `snapshot()` 拷出再在 router 里遍历，session 锁只覆盖拷贝。
- **淘汰前提核实（提案遗留项）**：`TimeWindow` 按 `ts` 非递减追加才能及时淘汰。每个 run 的写回按 ts 有序：decoder 按解码序入 READY → dispatcher 单线程 FIFO 取帧、同一 run 只进一个 stage deque、按序组批提交 → 推理子进程单进程串行消费（`stage_worker.run_stages`）→ 单收集线程按应答序写回。子进程崩溃只会丢掉在途批，不会改变后续帧的相对顺序。已写进 topic 声明注释。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 算子框架：10s 底线、按感受野放宽、per-operator 隔离（`test_operator_framework`） | passed |
| task 消息组装（`test_task_message_assembler`、`test_task_message_api`）、汇总 10s 裁窗（`test_alarm_increment::test_window_summary`） | passed |
| 全量 `pytest tests/` | 1212 passed |

## 遗留风险 / 后续任务

无（批 5a 迁帧队列）。
