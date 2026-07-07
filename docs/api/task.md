# `/task` — 前端消息与告警历史

两个 GET：`message` 走**运行时内存增量**（前端轮询用），`alarms` 走**数据库历史**。通用约定见 [README](README.md)。

---

## GET /task/message/{task_id}

前端实时告警消息（按 seq 增量 + 近 10s 信号）。适合前端 1–2 Hz 轮询。数据来自内存（非 DB）。

**路径参数**：`task_id`（int）。
**查询参数**：`since_seq`（int，默认 0）—— 游标，只返回 `seq > since_seq` 的告警。`since_seq < 0` → **400**。

**200**：

```jsonc
{
  "task_id": 123,
  "max_seq": 42,                          // 当前最大告警 seq；下次请求带 since_seq=max_seq 取增量
  "signals_10s": {                        // 近 10s 按 metric 聚合的实时信号；键 = AlarmMetric 值（仅 realtime 规则的流）
    "BUBBLE": { "active": true, "hit_count": 3, "max_conf": 0.95 }
    // 含全量空模板：未命中的 metric 为 {active:false, hit_count:0, max_conf:0.0}
  },
  "alarms": [                             // seq > since_seq 的增量告警
    {
      "seq": 40,
      "mode": "REALTIME",                 // REALTIME | SETTLEMENT
      "metric": "BUBBLE",                 // AlarmMetric 值
      "level": "high",                    // low | medium | high | critical
      "message": "持续产生新气泡…疑似漏气",
      "ts": 1751800000000                 // epoch 毫秒
    }
  ]
}
```

**无活跃 run**：返回空模板 —— `max_seq: 0`、`alarms: []`、`signals_10s` 全零模板（不报错）。

> 游标用法：客户端保存返回的 `max_seq`，下次以 `since_seq=max_seq` 请求，避免重复。字段名较短（`metric`/`level`/`message`），与 `/admin` 的告警字段名不同。

---

## GET /task/{task_id}/alarms

该任务的**全部**告警历史（始终查 `clean_alarm` 表，DESC）。较实时告警有秒级延迟（AlarmWorker 异步写库）。

**路径参数**：`task_id`（int）。

**200**：

```jsonc
{
  "task_id": 123,
  "total": 5,
  "alarms": [
    {
      "alarm_id": 1001,
      "task_id": 123,
      "step_id": 10,
      "step_name": "泄漏检测",
      "alarm_type": "流程违规",           // 流程违规 | 任务超时 | mock_alarm
      "severity": "high",                 // low | medium | high | critical
      "message": "...",
      "resolved": false,
      "resolved_by": null,
      "detected_at": 1751800000000,       // epoch 毫秒，可为 null
      "resolved_at": null                 // epoch 毫秒，可为 null
    }
  ]
}
```

**错误**：`503`（DB 查询失败，retryable）。
