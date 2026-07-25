# 告警数据流说明

本文档说明告警从产生到消费的完整路径，包括前端实时推送与后台数据库上报两条分支、
任务结束时的结算落盘逻辑，以及门控去重机制。

---

## 1. 架构概述

告警采用**双写架构**：每条通过门控的告警同时写入两个目的地，互不阻塞。

```text
推理线程（1Hz 时序分析）
  └─ AlarmInfo（alarm_type / alarm_level / alarm_message / metadata）
       │
       ▼
  try_pass_alarm_gate()  ← 5 秒冷却，key = task_id:metric:mode
       │ 通过
       ├─► append_alarm_record()  →  _alarm_log（内存环形缓冲）  ← 分支A：前端实时
       └─► persist_alarm()        →  alarm_queue（异步队列）      ← 分支B：后台上报
```

任务结束时（SETTLEMENT 阶段）产生的结算告警走相同路径，仅 `mode` 字段标记为 `"SETTLEMENT"`。

---

## 2. 告警门控（Alarm Gate）

**位置**：`app/services/client/queues.py:try_pass_alarm_gate()`

| 属性 | 值 |
| --- | --- |
| 冷却窗口 | 5 秒 |
| 去重 key | `f"{task_id}:{metric}:{mode}"` |
| 通过条件 | 该 key 上次通过时间距现在 > 5s |
| 失败行为 | 丢弃本次告警，不写入任何目的地 |

key 示例：`"123:BUBBLE:REALTIME"`、`"123:BENDING:SETTLEMENT"`

同一 task_id 下，REALTIME 与 SETTLEMENT 各有独立冷却计数，互不影响。

---

## 3. 分支 A：前端实时路径

```text
append_alarm_record(AlarmRecord)
  └─► ClientQueues._alarm_log
        ├─ 环形缓冲，maxlen = 100
        ├─ 写入时自增 _alarm_seq，赋值给 record.seq
        │
        ├─► GET /task/message/{task_id}?since_seq=<n>   （轮询，增量消费）
        └─► WS  /task/msg/{client_id}                   （1Hz 推送，最近5条）
```

**AlarmRecord 字段**（内存层）：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `seq` | int | 单任务内递增序号，由写入时自动赋值 |
| `alarm_type` | str | `"流程违规"` / `"任务超时"` |
| `alarm_level` | str | `low` / `medium` / `high` / `critical` / `warning` |
| `alarm_message` | str | 人可读告警描述 |
| `mode` | str | `REALTIME` 或 `SETTLEMENT` |
| `metric` | str | `BUBBLE` / `BENDING` / `TASK_TIMEOUT` / `UNKNOWN` |
| `stage` | str | `LEAK` 或 `CLEAN` |
| `timestamp` | float | Unix 时间戳（秒） |
| `metadata` | dict | 额外检测指标（如 `birth_rate`、`bend_actions`） |

---

## 4. 分支 B：后台上报路径

```text
persistence_manager.persist_alarm(alarm_dict)
  └─► alarm_queue（内存异步队列）
        └─► AlarmWorkerPool
              └─► AlarmWorker._process()
                    └─► GuardedExecutor（最多 3 次重试）
                          └─► AlarmPersistenceStrategy._send_alarm_http()
                                └─► HTTP POST settings.alarm_report_url
```

**上报条件**：`alarm_dict` 中 `task_id` 非空且 `step_id` 不为 None；否则跳过 HTTP 发送（不报错）。

**HTTP 请求格式**（`alarm_strategy.py:_send_alarm_http()`）：

```http
POST <alarm_report_url>
Content-Type: application/json; charset=utf-8
User-Agent: CleanSight-Backend/1.0

{
  "task_id": 123,
  "step_id": 2,
  "alarm_type": "流程违规",
  "alarm_level": "high",
  "alarm_message": "持续产生新气泡（birth_rate=0.85），疑似漏气",
  "alarm_time": "2026-04-26 14:30:00",
  "detection_result": {"birth_rate": 0.85, "threshold": 0.5}
}
```

`detection_result` 仅在 `alarm_dict["detection_result"]` 有值时携带。

**平台响应**（成功）：

```json
{"code": 0}
```

---

## 5. 任务结束结算（SETTLEMENT）

**触发时机**：

| 触发点 | 场景 |
| --- | --- |
| `InferenceManager.set_task()` | 任务切换（当前阶段结束，新阶段开始） |
| `InferenceManager.remove_client()` | 客户端断开 / `POST /api/terminate` |

**调用链**：

```text
set_task() / remove_client()
  └─ old_actor.finalize_and_stop()
        └─ analyzer.finalize()  →  List[AlarmInfo]  （各子分析器 override）
              └─ _persist_settlement_alarms()
                    ├─ try_pass_alarm_gate(metric, mode="SETTLEMENT")  ← 同样走门控
                    ├─ persistence_manager.persist_alarm(...)          ← 后台上报
                    └─ cq.append_alarm_record(AlarmRecord(mode="SETTLEMENT", ...))
```

结算告警与实时告警在两条路径中的处理逻辑完全相同，区别仅在于 `mode="SETTLEMENT"`，
前端可据此区分弹窗样式（结算摘要 vs 实时警告）。

**示例**：`DebounceAnalyzer.finalize()` 在弯曲次数不足时产生：

```text
alarm_type:    "流程违规"
alarm_level:   "warning"
alarm_message: "弯曲动作不足：完成 1 次，要求 3 次"
mode:          "SETTLEMENT"
metric:        "BENDING"
```

---

## 6. 枚举参考

| 枚举 | 当前值 | 说明 |
| --- | --- | --- |
| `AlarmType` | `"流程违规"` / `"任务超时"` | HTTP 上报与 alarm_type 字段用中文 value |
| `AlarmMetric` | `BUBBLE` / `BENDING` / `TASK_TIMEOUT` / `UNKNOWN` | 门控 key 与 API 响应 metric 字段 |
| `AlarmMode` | `REALTIME` / `SETTLEMENT` | 推理阶段实时产生 vs 任务结束结算 |
| `alarm_level` | `low` / `medium` / `high` / `critical` / `warning` | `warning` 仅出现在结算告警 |
