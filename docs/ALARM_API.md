**告警实时接口说明（V1）**

**1. 接口**
- `GET /task/message/{task_id}?since_seq=<int>`

**2. 请求参数**
- `task_id`（path，必填，int）：任务 ID
- `since_seq`（query，可选，int，默认 `0`）：前端已消费到的告警序号

**3. 返回格式（200）**
```json
{
  "task_id": 123,
  "max_seq": 152,
  "signals_10s": {
    "BUBBLE": { "active": true, "hit_count": 8, "max_conf": 0.73 },
    "BENDING": { "active": false, "hit_count": 0, "max_conf": 0.0 },
    "TASK_SETTLEMENT": { "active": false, "hit_count": 0, "max_conf": 0.0 }
  },
  "alarms": [
    {
      "seq": 151,
      "mode": "REALTIME",
      "metric": "BUBBLE",
      "level": "high",
      "message": "气泡率超阈值",
      "count": 3,
      "ts": 1760000000
    }
  ]
}
```

**4. 字段语义**
- `signals_10s`：最近 10 秒检测信号摘要（非告警，不用于弹窗）
- `alarms`：增量告警列表，仅返回 `seq > since_seq`
- `max_seq`：当前任务最大告警序号，前端用于推进游标

**5. 前端消费约定**
- 首次请求传 `since_seq=0`
- 每次成功后将本地游标更新为 `max_seq`
- 告警提示只基于 `alarms`
- 建议轮询频率：`1s`

**6. 枚举定义（V1）**
- `metric`：`BUBBLE | BENDING | TASK_SETTLEMENT`
- `mode`：`REALTIME | SETTLEMENT`
- `level`：`low | medium | high | critical`

**7. 错误码**
- `404`：`task_id` 不存在
- `400`：参数不合法（如 `since_seq < 0`）
- `500`：服务内部异常

**8. 聚合规则**
- 写入前 5 秒去重聚合
- 同键（`task_id + metric + mode + stage`）在 5 秒内合并为一条，`count` 累加
