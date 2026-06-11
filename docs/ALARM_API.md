# 告警实时接口说明

## 1. 接口

`GET /task/message/{task_id}?since_seq=<int>`

## 2. 请求参数

- `task_id`（path，必填，int）：任务 ID
- `since_seq`（query，可选，int，默认 `0`）：前端已消费到的告警序号

## 3. 返回格式（200）

```json
{
  "task_id": 123,
  "max_seq": 5,
  "signals_10s": {
    "BUBBLE":  {"active": true,  "hit_count": 8, "max_conf": 0.7321},
    "BENDING": {"active": false, "hit_count": 0, "max_conf": 0.0}
  },
  "alarms": [
    {
      "seq": 3,
      "mode": "REALTIME",
      "metric": "BUBBLE",
      "level": "high",
      "message": "持续产生新气泡（birth_rate=0.85），疑似漏气",
      "ts": 1760000100
    },
    {
      "seq": 4,
      "mode": "SETTLEMENT",
      "metric": "BENDING",
      "level": "warning",
      "message": "弯曲动作不足：完成 1 次，要求 3 次",
      "ts": 1760000200
    }
  ]
}
```

## 4. 字段语义

- `signals_10s`：最近 10 秒检测信号摘要（原始检测频次统计，非告警，不用于弹窗）
- `alarms`：增量告警列表，仅返回 `seq > since_seq` 的条目
- `max_seq`：当前任务最大告警序号，前端用于推进游标

## 5. 前端消费约定

- 首次请求传 `since_seq=0`
- 每次成功后将本地游标更新为 `max_seq`
- 告警提示只基于 `alarms`，不基于 `signals_10s`
- 建议轮询频率：`1s`

## 6. 枚举定义

- `metric`：`BUBBLE | BENDING | TASK_TIMEOUT | UNKNOWN`
- `mode`：`REALTIME | SETTLEMENT`
- `level`：`low | medium | high | critical | warning`

## 7. 错误码

- `404`：`task_id` 不存在（任务从未启动）
- `400`：参数不合法（如 `since_seq < 0`）
- `500`：服务内部异常

任务已结束但 `task_id` 有效时，接口返回空 `alarms` 列表而非 404，历史记录请查 `/task/{task_id}/alarms`。

## 8. 告警门控规则

同一组合 `(task_id, metric, mode)` 在 5 秒内至多写入一条告警到内存日志，超出部分被丢弃。
门控 key 格式：`"{task_id}:{metric}:{mode}"`，REALTIME 与 SETTLEMENT 计数独立。

## 9. 与 `/task/{task_id}/alarms` 的区别

| 维度 | `GET /task/message/{task_id}` | `GET /task/{task_id}/alarms` |
| --- | --- | --- |
| 数据来源 | 内存环形缓冲（最近 100 条） | 活跃任务读内存；已结束任务查数据库 |
| 消费方式 | 增量（`since_seq` 游标） | 全量返回 |
| 字段风格 | `seq/mode/metric/level/message/ts` | `alarm_id/alarm_type/severity/message/detected_at` |
| 适用场景 | 前端实时告警弹窗、轮询 | 任务历史回溯、管理后台 |
