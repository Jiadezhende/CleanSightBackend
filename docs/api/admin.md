# `/admin-f3m8` — 运维 Admin

运维面板 API，均无鉴权、返回 200。前缀含混淆串。静态 UI：`GET /admin-f3m8/ui`（SPA）。通用约定见 [README](README.md)。

> 告警字段名用**全称**（`alarm_type`/`alarm_level`/`alarm_message`），与 `/task/message` 的短名（`metric`/`level`/`message`）不同。

---

## GET /admin-f3m8/overview

活跃任务 + 队列深度聚合（适合 ~3s 轮询）。

```jsonc
{
  "timestamp": 1751800000,          // epoch 秒
  "active_clients": 2,
  "total_queued_frames": 137,
  "clients": [
    { "client_id": 123, "task_id": 123, "source_ip": "192.168.1.100", "step_id": 10,
      "queue_depths": { "ca_raw": 30, "ca_ready": 5, "ca_processed": 12 } }
  ]
}
```

## GET /admin-f3m8/clients

轻量客户端列表（UI 下拉用），元素同 overview 的 `clients[]`。

## GET /admin-f3m8/clients/{client_id}/alarms

某 client 内存中的近 n 条告警。

**路径**：`client_id`（int，= task_id）。**查询**：`n`（int，默认 20，范围 1–100）。

```jsonc
{
  "client_id": 123,
  "alarms": [
    { "seq": 40, "alarm_type": "流程违规", "alarm_level": "high", "alarm_message": "...",
      "mode": "REALTIME", "metric": "BUBBLE", "stage": "LEAK", "timestamp": 1751800000 }
  ],
  "error": null                     // 未找到 client 时为 "client_not_found"，alarms 为 []
}
```

## GET /admin-f3m8/metrics/json

Prometheus 指标解析为结构化 JSON：`infer_latency_ms`（按模型 p50/p95/p99/total_count）、`infer_failure_total`（total + by_type）、`frame_drop_total`（total + by_reason）、`gpu_oom_total`、`retry_total`（total + by_operation）。解析异常返回 `{}`。

## GET /admin-f3m8/ping

延迟探针：`{"server_time_ms": 1751800000000.0}`（客户端算 RTT）。
