# WS /task/msg/{client_id} 接口文档

每秒推送一次，提供实时检测结果与告警信息。

---

## 正常推送

```json
{
  "stage": "LEAK",
  "detections": {
    "bubble": true,
    "bending": false
  },
  "recent_alarms": [
    {
      "alarm_type": "BUBBLE_DETECTED",
      "alarm_level": "ERROR",
      "alarm_message": "检测到漏气",
      "timestamp": 1741234560.0
    }
  ]
}
```

| 字段 | 类型 | 说明 |
|------|------|------|
| `stage` | string | 当前检测阶段代码，如 `LEAK`、`CLEAN` |
| `detections` | object | 各检测项结果，`true` 表示检测到异常 |
| `recent_alarms` | array | 最近 5 条内存告警，最新在后 |
| `recent_alarms[].alarm_level` | string | `ERROR` 或 `WARN` |
| `recent_alarms[].timestamp` | number | Unix 时间戳（秒） |

---

## 异常情况

```json
{ "error": "Client '192.168.1.100' not found" }
```

client 不存在时每秒推送一次，前端可据此显示"任务未启动"。

---

## 与旧接口的字段对照

| 旧 `WS /task/status` | 新 `WS /task/msg` |
|----------------------|-------------------|
| `detection.bubble_detected` | `detections.bubble` |
| `detection.bending` | `detections.bending` |
| `messages[]` | `recent_alarms[].alarm_message` |
| `status`、`cleaning_step`、`task_id` | 已移除，仍从 `WS /task/status` 获取 |
