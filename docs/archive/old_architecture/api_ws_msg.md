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
      "alarm_type": "流程违规",
      "alarm_level": "high",
      "alarm_message": "持续产生新气泡（birth_rate=0.85），疑似漏气",
      "timestamp": 1760000100.0,
      "mode": "REALTIME",
      "metric": "BUBBLE"
    }
  ]
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `stage` | string | 当前检测阶段，如 `LEAK`、`CLEAN` |
| `detections` | object | 各检测项最新一帧结果，`true` 表示检测到目标 |
| `recent_alarms` | array | 最近 5 条内存告警，最新在后 |
| `recent_alarms[].alarm_type` | string | 告警类型，中文值，如 `"流程违规"`、`"任务超时"` |
| `recent_alarms[].alarm_level` | string | `low` / `medium` / `high` / `critical` / `warning` |
| `recent_alarms[].alarm_message` | string | 人可读告警描述 |
| `recent_alarms[].timestamp` | number | Unix 时间戳（秒，float） |
| `recent_alarms[].mode` | string | `REALTIME`（推理中产生）或 `SETTLEMENT`（任务结束结算） |
| `recent_alarms[].metric` | string | `BUBBLE` / `BENDING` / `TASK_TIMEOUT` / `UNKNOWN` |

---

## 异常情况

```json
{"error": "Client '192.168.1.100' not found"}
```

client 不存在时每秒推送一次，前端可据此显示"任务未启动"。

---

## 与 GET /task/message/{task_id} 的区别

| 维度 | WS /task/msg | GET /task/message |
| --- | --- | --- |
| 标识符 | `client_id`（摄像机 IP） | `task_id`（业务任务 ID） |
| 推送方式 | 服务端主动 1Hz 推送 | 客户端轮询，增量游标 |
| 告警字段 | `alarm_type/alarm_level/alarm_message/timestamp/mode/metric` | `seq/mode/metric/level/message/ts` |
| 告警范围 | 最近 5 条 | `seq > since_seq` 的全部增量 |
| 适用场景 | 低延迟实时展示 | 可靠增量消费，防漏条 |

---

## 与旧接口的字段对照

| 旧 `WS /task/status` | 新 `WS /task/msg` |
| --- | --- |
| `detection.bubble_detected` | `detections.bubble` |
| `detection.bending` | `detections.bending` |
| `messages[]` | `recent_alarms[].alarm_message` |
| `status`、`cleaning_step`、`task_id` | 已移除，仍从 `WS /task/status` 获取 |
