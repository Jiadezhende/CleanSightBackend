# RTSP 全流程调用说明（CleanSightBackend）

> **版本**: 2.0（2026-04-13 更新）

## 概述

演示：推流端（FFmpeg）将视频推到 MediaMTX；后端通过统一 API `/api/start` 加载任务并启动拉流解码；前端通过 WebSocket `/ai/video` 订阅实时可视化帧。

**关键概念**：

- `client_id`：系统中标识流/客户端的字符串，由 `clean_task.source_ip` 提供。
- `task_id`：任务表主键，启动时从数据库读取任务配置。
- `rtsp_url`：推流地址（MediaMTX 对外的 RTSP 地址）。

## 网络拓扑（2026-04 起）

```text
推流端 FFmpeg ─────▶ 8004 (mediamtx_gateway)  ─▶  127.0.0.1:18004 (MediaMTX)
                      IP 白名单 / 速率限制                │
                                                         ▼
                    ┌────────────────────────────────────┐
后端拉流 ──────────▶│ 内部直连 127.0.0.1:18004（绕过 8004）│
                    └────────────────────────────────────┘
```

- **对外**：MediaMTX 的 RTSP 端口 8004 由 [mediamtx_gateway](../mediamtx_gateway/) 反代，原始 MediaMTX 只监听 `127.0.0.1:18004`。
- **后端拉流**：`start_stream()` 中的 `_rewrite_rtsp_url()` 会自动把 URL 中的 8004 端口重写为 18004，绕过代理直连 MediaMTX（避免自我 IP 被代理误封）。
- **Gateway 部署**：生产建议 `mediamtx_bin` 留空让 MediaMTX 由 systemd/容器独立管理；开发可填写 bin 路径由 Gateway 守护。详见 [API_GATEWAY.md](API_GATEWAY.md)。

## 主要接口

> `/docs` `/redoc` `/openapi.json` 已永久关闭；`/inspection/*` `/ai/load_task` `/ai/status` `/ai/terminate_task` 均已移除。

### POST `/api/start`

一步完成"加载任务 + 启动拉流解码 + 关联 ClientManager"。

**请求 JSON**：

```json
{
  "task_id": 1,
  "rtsp_url": "rtsp://<public-host>:8004/live/172.16.77.221",
  "fps": 30
}
```

`client_id` 从 `clean_task.source_ip` 自动读取，必须与 rtsp_url 的 path（`/live/<client_id>`）一致。

**成功响应**：200 + `{"status":"success", "client_id":"..."}`。

**常见错误**：

- 404 — 未找到 task
- 400 — `source_ip` 为空
- 409 — `client_id` 已有存活流（需先调 `/api/terminate`）
- 503 — 暂时拉不到流（MediaMTX 路径尚未就绪；健康监控会继续重连，无需调用方重试）

### POST `/api/terminate?client_id=...`

完整清理：停止解码器 → 停止推理 → 从 ClientManager 注销。

**示例**：

```powershell
Invoke-RestMethod -Method Post -Uri "http://<host>:8000/api/terminate?client_id=172.16.77.221"
```

### WebSocket `/ai/video?client_id=...`

订阅实时可视化帧，文本消息 `data:image/jpeg;base64,<b64>`。服务端内置 `_recv_until_disconnect()` 监听客户端 CLOSE 帧，配合 lifespan 优雅关闭；客户端主动断开后服务端会清理对应资源。

### GET `/health/monitor/stats`（健康监控观察）

查询健康监控统计：`reconnects` / `reconnect_successes` / `orphans_detected` / `reconnecting_clients`。

`/health/*` 走 Gateway **宽松路径 bucket**（高配额、不计入封禁升级与反扫描），轮询安全。

## 端到端调用顺序（推荐）

1. 确认 `clean_task` 表中存在目标任务，`source_ip` 填为期望的 `client_id`（例如 `172.16.77.221`）。
2. 推流端启动 FFmpeg：

   ```powershell
   ffmpeg -an -re -stream_loop -1 -i test_video.mp4 \
     -c:v libx264 -preset ultrafast -tune zerolatency \
     -rtsp_transport tcp -f rtsp \
     rtsp://<public-host>:8004/live/172.16.77.221
   ```

3. 调用 `/api/start`：

   ```powershell
   $body = @{ task_id=1; rtsp_url="rtsp://<public-host>:8004/live/172.16.77.221"; fps=30 } | ConvertTo-Json
   Invoke-RestMethod -Method Post -Uri "http://<host>:8000/api/start" -Body $body -ContentType "application/json"
   ```

4. （可选）WebSocket 订阅 `/ai/video?client_id=172.16.77.221`。
5. 运行结束后调 `/api/terminate`。

## 关键行为（2026-04 起）

- **首次 `/api/start` 若 FFmpeg 未连上 MediaMTX**：接口返回成功（decoder 已注册），流未立即就绪；`StreamHealthMonitor` 5s 后检测到 `is_alive=False` 发起重连（最多 5 次）。推流端后启动也能被自动捕捉。详见 [STREAM_RECONNECT_IMPLEMENTATION.md](STREAM_RECONNECT_IMPLEMENTATION.md)。
- **背压**：推理积压时只丢 `ca_ready`，`ca_raw`（HLS 录制）继续写入；HLS 分段不受推理积压影响。
- **日志级别**：瞬态连接失败（推流端未就绪 / 网络抖动）记 WARNING；真正 FFmpeg 崩溃（二进制错误 / 不支持格式）记 ERROR。

## 故障排查

- **推流端被网关封禁**：查看后端日志 `[RTSPProxy] Blocked ... <ip>`；调整 `mediamtx_gateway/config.ini` 的 `allowed_ips`、`rate_limit`，或等待 `ban_duration`（默认 3600s）过期。
- **后端拉流失败**：查看 FFmpeg stderr（decoder 已按瞬态 vs 真实崩溃分类打日志）；确认 rtsp_url 的端口与 `CLEANSIGHT_MEDIAMTX_PROXY_PORT` 一致（默认 8004）。
- **`/api/start` 返回 409**：`client_id` 已有存活流，先调 `/api/terminate`。
- **IPv6 / localhost 问题**：Windows 下 `localhost` 可能解析为 `::1`，MediaMTX 只绑 127.0.0.1；代码已在 URL 重写时自动把 `localhost` 替换为 `127.0.0.1`，外部调用端仍建议使用 IPv4 地址。

## 相关文档

- [API_GATEWAY.md](API_GATEWAY.md) - RTSP TCP 代理 + HTTP/WS 中间件
- [STREAM_RECONNECT_IMPLEMENTATION.md](STREAM_RECONNECT_IMPLEMENTATION.md) - 断线重连机制
- [API_ENDPOINTS.md](API_ENDPOINTS.md) - 全部接口清单
