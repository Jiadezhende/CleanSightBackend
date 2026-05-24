> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# API Surface

本文件列出当前代码注册的主要 HTTP 和 WebSocket 接口。

## 统一任务 API

- `POST /api/start`：启动任务并启动流。
- `POST /api/terminate?client_id=...`：终止任务并清理 client。

来源：`app/routers/api.py`

## 实时推理

- `WebSocket /ai/video?client_id=...`：推送最新渲染 JPEG，文本消息为 data URL。

来源：`app/routers/ai.py`

## 任务消息

- `GET /task/{task_id}/alarms`：查询数据库历史告警。
- `GET /task/message/{task_id}?since_seq=...`：查询运行时内存增量消息。

来源：`app/routers/task.py`

## 追溯

- `GET /traceback/alarm/{alarm_id}/evidence`
- `GET /traceback/task/{task_id}/playlist.m3u8?step_id=...&track=raw|processed`
- `GET /traceback/alarm/{alarm_id}/playlist.m3u8?track=raw|processed`
- `GET /traceback/task/{task_id}/timeline?step_id=...`

来源：`app/routers/traceback.py`

## 媒体访问

- `GET /media/segment/{token}`
- `GET /media/init/{token}`
- `GET /media/keypoints/{token}`

所有 URL 由 HMAC token 鉴权。

来源：`app/routers/media.py`

## Lab

- `POST /lab-f3m8/submit`
- `GET /lab-f3m8/health`
- `GET /lab-f3m8/config`
- `PUT /lab-f3m8/config`
- 静态 UI：`/lab-f3m8/ui`

来源：`app/routers/lab.py`、`app/main.py`

## Admin 与 Health

- `GET /admin-f3m8/overview`
- `GET /admin-f3m8/clients`
- `GET /admin-f3m8/clients/{client_id}/alarms`
- `GET /admin-f3m8/metrics/json`
- `GET /admin-f3m8/ping`
- 静态 UI：`/admin-f3m8/ui`
- `GET /health/monitor/stats`
- `GET /health/monitor/config`
- `GET /health/status`

来源：`app/routers/admin.py`、`app/routers/health.py`

## Gateway 行为

HTTP 与 WebSocket 进入路由前会经过 GatewayMiddleware。`/media` 默认绕过速率限制和反扫描，但仍检查 IP 白名单和封禁。

来源：`app/utils/gateway.py`

