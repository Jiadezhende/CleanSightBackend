# `/health` — 健康与监控

三个 GET，均无鉴权、返回 200。健康监控未就绪时返回 `{"status": "not_initialized", "message": "..."}`。通用约定见 [README](README.md)。

---

## GET /health/status

系统整体实时快照。

```jsonc
{
  "status": "running",
  "clients": {
    "total_clients": 2,        // 有队列的 client 数
    "active_streams": 2,       // 有 decoder 且非重连中
    "reconnecting": 0,
    "orphan_streams": 0,       // 有队列无 decoder
    "orphan_decoders": 0       // 有 decoder 无队列
  },
  "queues": { "<client_id>": { "raw_queue_size": 0, "ready_queue_size": 0, "latest_timestamp": 0, "...": 0 } },
  "monitor_stats": { "checks": 0, "suspects": 0, "cleanups": 0, "reconnects": 0,
                     "reconnect_successes": 0, "orphans_detected": 0, "reconnecting_clients": [] }
}
```

---

## GET /health/monitor/stats

健康监控计数（累计）+ 重连实时快照。

```jsonc
{
  "status": "running",
  "checks": 100, "suspects": 3, "cleanups": 1, "reconnects": 2,
  "reconnect_successes": 2, "orphans_detected": 0,
  "reconnecting_count": 0, "reconnecting_clients": []   // 实时快照
}
```

---

## GET /health/monitor/config

健康监控配置参数。

```jsonc
{
  "status": "running",
  "config": {
    "check_interval": 5.0, "heartbeat_timeout": 10.0, "reconnect_interval": 3.0,
    "max_reconnect_attempts": 5, "orphan_timeout": 30.0
  },
  "derived": { "suspect_timeout": 5.0, "cleanup_timeout": 30.0 }
}
```
