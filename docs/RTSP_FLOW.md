# RTSP 全流程调用说明（CleanSightBackend）

> **版本**: 2.2（2026-04-16 更新）

## 概述

实际部署场景：海康威视摄像头位于**现场局域网**，服务器部署在外网。由于两者不在同一网段，需要在现场运行一个 FFmpeg 进程完成网络穿越：先从摄像头拉取 RTSP 流，再转推到服务器的 MediaMTX；后端通过 `/api/start` 启动任务并从 MediaMTX 拉取；前端通过 WebSocket `/ai/video` 订阅实时可视化帧。

**关键概念**：

- `client_id`：系统中标识流/客户端的字符串，由数据库 `clean_task.source_ip` 字段提供，与推流地址无直接绑定关系。
- `task_id`：任务表主键，启动时从数据库读取任务配置。
- `rtsp_url`：传给 `/api/start` 的 MediaMTX 对外地址，需与推流端实际推送路径一致（见下方约定说明）。

## 网络拓扑

```text
【现场局域网】                              【服务器】
海康摄像头(:554)
    │ 局域网内拉流
    ▼
本地 FFmpeg 进程 ──────────────────▶  :8004 (mediamtx_gateway)
                  RTSP 转推（跨网）      IP 白名单 / 速率限制
                                              │
                                              ▼
                                       127.0.0.1:18004 (MediaMTX)
                                              │ 内部直连（绕过 8004）
                                              ▼
                                        FFmpegDecoder（后端拉流）
```

- **对外**：MediaMTX 的 RTSP 端口 8004 由 [mediamtx_gateway](../mediamtx_gateway/) 反代，原始 MediaMTX 只监听 `127.0.0.1:18004`。
- **后端拉流**：`start_stream()` 中的 `_rewrite_rtsp_url()` 会自动把端口 8004 重写为 18004，并将 host **统一替换为 `127.0.0.1`**（无论客户端传入的是 `localhost`、外网 IP 还是其他地址），确保 FFmpeg 始终走 loopback 直连 MediaMTX，不经过外网网卡。
- **Gateway 部署**：生产建议 `mediamtx_bin` 留空让 MediaMTX 由 systemd/容器独立管理；开发可填写 bin 路径由 Gateway 守护。详见 [API_GATEWAY.md](API_GATEWAY.md)。

## 流路径命名约定（隐式耦合说明）

推流端推送的 MediaMTX 路径（如 `/live/xxx`）与 `task_id` / `client_id` 之间**没有代码强制约束**，完全依赖调用方自行保证一致性：

- 推流端推到 `rtsp://服务器IP:8004/live/xxx`
- `/api/start` 中 `rtsp_url` 必须传 `rtsp://服务器IP:8004/live/xxx`（路径相同）
- `client_id` 来自数据库 `source_ip` 字段，与路径名无关

**推荐命名规则**：使用 `source_ip` 作为路径名（如 `/live/172.16.77.221`），与 `client_id` 保持一致，便于日志定位。若使用 `task_id` 作为路径名，需注意同一摄像头切换任务时路径也需随之更新。

路径名不匹配不会报错，但会导致后端拉到错误的流并将推理结果写入错误的任务记录。

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

## 端到端调用顺序（生产场景：海康摄像头）

### 前提条件

- `clean_task` 表中存在目标任务，且 `source_ip` 字段已填写摄像头所在网络的出口 IP（即 `client_id`，例如 `172.16.77.221`）。
- 现场机器能访问摄像头（局域网），也能访问服务器的 8004 端口。
- 服务器防火墙已放行 8004/TCP（MediaMTX 对外 RTSP 端口）。

### 步骤

#### 1. 在现场机器上启动推流 FFmpeg

从海康摄像头拉流并转推到服务器 MediaMTX：

```bash
ffmpeg -loglevel info -hide_banner \
  -rtsp_transport tcp \
  -probesize 2000000 \
  -analyzeduration 2000000 \
  -buffer_size 10485760 \
  -i "rtsp://admin:<password>@<camera-ip>:554/Streaming/Channels/401" \
  -vcodec copy \
  -an \
  -f rtsp \
  -rtsp_transport tcp \
  -flags +global_header \
  -fflags +genpts \
  "rtsp://<server-ip>:8004/live/172.16.77.221"
```

- `Streaming/Channels/401`：第 4 通道主码流；按需改为 `101`（通道 1）等。
- 路径 `/live/172.16.77.221`：建议使用 `source_ip` 作为路径名，与 `client_id` 对应。
- 推流成功后 MediaMTX 日志会出现 `[path /live/172.16.77.221] publisher connected`。

#### 2. 调用 `/api/start`

`rtsp_url` 必须与推流端路径完全一致：

```bash
curl -X POST http://<server-ip>:8000/api/start \
  -H "Content-Type: application/json" \
  -d '{"task_id": 1, "rtsp_url": "rtsp://<server-ip>:8004/live/172.16.77.221", "fps": 30}'
```

#### 3. （可选）WebSocket 订阅实时画面

```text
ws://<server-ip>:8000/ai/video?client_id=172.16.77.221
```

#### 4. 结束任务

```bash
curl -X POST "http://<server-ip>:8000/api/terminate?client_id=172.16.77.221"
```

随后停止现场 FFmpeg 进程（Ctrl-C）。

---

### 开发/测试场景（本地 mp4 文件模拟推流）

```bash
ffmpeg -an -re -stream_loop -1 -i test_video.mp4 \
  -c:v libx264 -preset ultrafast -tune zerolatency \
  -rtsp_transport tcp -f rtsp \
  rtsp://<server-ip>:8004/live/172.16.77.221
```

## 关键行为（2026-04 起）

- **首次 `/api/start` 若 FFmpeg 未连上 MediaMTX**：接口返回成功（decoder 已注册），流未立即就绪；`StreamHealthMonitor` 5s 后检测到 `is_alive=False` 发起重连（最多 5 次）。推流端后启动也能被自动捕捉。详见 [STREAM_RECONNECT_IMPLEMENTATION.md](STREAM_RECONNECT_IMPLEMENTATION.md)。
- **背压**：推理积压时只丢 `ca_ready`，`ca_raw`（HLS 录制）继续写入；HLS 分段不受推理积压影响。
- **日志级别**：瞬态连接失败（推流端未就绪 / 网络抖动）记 WARNING；真正 FFmpeg 崩溃（二进制错误 / 不支持格式）记 ERROR。

## 故障排查

- **推流端被网关封禁**：查看后端日志 `[RTSPProxy] Blocked ... <ip>`；调整 `mediamtx_gateway/config.ini` 的 `allowed_ips`、`rate_limit`，或等待 `ban_duration`（默认 3600s）过期。
- **后端拉流失败**：查看 FFmpeg stderr（decoder 已按瞬态 vs 真实崩溃分类打日志）；确认 rtsp_url 的端口与 `CLEANSIGHT_MEDIAMTX_PROXY_PORT` 一致（默认 8004）。
- **`/api/start` 返回 409**：`client_id` 已有存活流，先调 `/api/terminate`。
- **host 被重写为 127.0.0.1**：`_rewrite_rtsp_url()` 在端口匹配时会将任意 host（`localhost`、外网 IP 等）统一改写为 `127.0.0.1`。这是预期行为——后端拉流目标始终是本机 MediaMTX，走 loopback 可绕过云服务器 iptables 对 UDP 高位端口的拦截。`/api/start` 的 `rtsp_url` 传外网 IP 或 localhost 均可，rewrite 会统一处理。

## 相关文档

- [API_GATEWAY.md](API_GATEWAY.md) - RTSP TCP 代理 + HTTP/WS 中间件
- [STREAM_RECONNECT_IMPLEMENTATION.md](STREAM_RECONNECT_IMPLEMENTATION.md) - 断线重连机制
- [API_ENDPOINTS.md](API_ENDPOINTS.md) - 全部接口清单
