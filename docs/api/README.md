# CleanSight API 文档

对外 HTTP / WebSocket **端点契约**（请求/响应 schema、字段语义、错误码）。按 router 分文件；本页为索引 + 全局通用约定，各 router 文件不重复。

> 路由如何接线（router 归属、中间件与 lifespan 顺序）属架构，见知识库 [docs/kb/INDEX.md](../kb/INDEX.md)。

## 索引

| 文件 | 前缀 | 内容 |
|------|------|------|
| [api.md](api.md) | `/api` | 统一任务入口：启动 / 终止一次 run |
| [ai.md](ai.md) | `/ai` | 实时推理画面 WebSocket |
| [task.md](task.md) | `/task` | 前端增量消息 + 告警历史 + 大屏在线/历史任务清单 |
| [traceback.md](traceback.md) | `/traceback` | 告警证据 / VOD playlist / 时间轴 |
| [media.md](media.md) | `/media` | token 化媒体访问（段 / init.mp4） |
| [health.md](health.md) | `/health` | 健康状态与监控统计 |
| [admin.md](admin.md) | `/admin-f3m8` | 运维 Admin |
| [lab.md](lab.md) | `/lab-f3m8` | 送标导出 + Label Studio |

## 通用约定

### Base URL 与文档页

- 默认 `http://<host>:8000`。
- 生产已**永久关闭** `/docs`、`/redoc`、`/openapi.json`——无交互式文档页。

### Gateway

所有 HTTP/WS 进路由前先过 `GatewayMiddleware`：IP 白名单 → 限流 → 反扫描。`/media` 走放宽策略（绕过限流与反扫描，仍查 IP 白名单与封禁）。被拦截返回 403/429。

### 运行键与双模标识

运行键为 int `task_id`。业务端点 `/api/terminate`、`/ai/video` 双模兼容：

- `task_id`（int，**首选**）：O(1) 直取当前 run。
- `client_id`（str，旧，= `source_ip`）：扫描活跃 run 匹配首个命中；`/ai/video` 下会跟随该 source_ip 的当前 run。

`/api/start` 只接受 `task_id`（body）。

### 错误模型

业务异常经 FastAPI 全局 handler 映射为 HTTP（边界层 L3），响应体形如 `{"error": "...", "detail": "...", ...}`：

| 异常 | HTTP | 说明 |
|------|------|------|
| `ValidationError` | 400 | 参数缺失/非法（附 `field`） |
| `NotFoundError` | 404 | 资源不存在（附 `resource_type`/`resource_id`） |
| `ConflictError` | 409 | 资源状态冲突 |
| `StreamConnectionError` | 503 | RTSP 连接失败 |
| `DatabaseError` | 503 | 数据库不可用（`retryable: true`） |
| `FFmpegError` | 500 | 解码/编码失败 |
| `ModelInferenceError` | 500 | 推理失败 |
| `PersistenceError` | 500 | 落盘/HLS 写失败 |
| `AppError` | 500 | 通用内部错误（附 `retryable`） |
| 其他 | 500 | 未预期错误（不泄漏细节） |

部分端点为**尽力而为**（如 `/api/terminate`、`/health/*`、`/lab/health`）：不抛异常，用响应体字段表达部分失败。各文件单独标注。

### 时间戳单位

- 告警 `detected_at` / `resolved_at` / `ts`、追溯 `*_ms`：**epoch 毫秒**。
- 段文件 `ts_us`：微秒。
- 媒体 token 有效期：秒。

### 枚举取值

- `AlarmMetric`：`BUBBLE` / `BENDING` / `TASK_TIMEOUT` / `UNKNOWN`。
- `alarm_level`（severity）：`low` / `medium` / `high` / `critical`。
- `alarm_type`：`流程违规` / `任务超时` / `mock_alarm`。
- 告警 `mode`：`REALTIME` / `SETTLEMENT`。

> 契约以 `app/routers/*.py` 现码为准；字段变更请同步本目录。
