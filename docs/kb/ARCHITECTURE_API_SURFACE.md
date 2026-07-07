> 更新时间：2026-07-07
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# API 路由接线图

本文件描述 HTTP/WS 层的**架构接线**：有哪些 router、各自前缀与归属、注册与中间件顺序、生命周期挂载。**各端点的请求/响应契约、字段语义与调用示例属对外 API 文档范围，不在本知识库维护**（本库只回答「有哪些路由、谁拥有、怎么接线」）。

运行键为 int `task_id`；业务端点（`/api`、`/ai`）对 `task_id`（首选）与旧 `client_id`(=source_ip) 双模兼容，对外 wire 未变。

## Router 归属

| 前缀 | router | 职责（一句） |
|------|--------|------------|
| `/api` | `routers/api.py` | 统一任务入口：启动/终止一次 run（桥接 `RunController`） |
| `/ai` | `routers/ai.py` | 实时推理 WebSocket（渲染帧推送） |
| `/task` | `routers/task.py` | 任务消息与告警历史查询 |
| `/traceback` | `routers/traceback.py` | 告警证据 / VOD playlist / 时间轴溯源 |
| `/media` | `routers/media.py` | HMAC token 化媒体访问（段 / init.mp4） |
| `/health` | `routers/health.py` | 健康状态与监控统计 |
| `/lab-f3m8` | `routers/lab.py` | 送标导出 + Label Studio（含静态 UI） |
| `/admin-f3m8` | `routers/admin.py` | 运维 Admin（概览 / 客户端 / 指标，含静态 UI） |

唯一的 WebSocket 路由是 `/ai/video`；其余均为 HTTP。`/lab-f3m8/ui`、`/admin-f3m8/ui` 为 `StaticFiles` 挂载。

## 注册与中间件顺序

`app/main.py` 按序 `include_router`：api → health → ai → task → traceback → media → lab → admin（api 优先注册）。

中间件：`GatewayMiddleware` 在 CORS 之后 `add_middleware`；Starlette 逆序包装，故 **Gateway 最先执行**——所有 HTTP/WS 进路由前先过 IP 白名单 / 限流 / 反扫描。`/media` 默认走放宽策略（绕过限流与反扫描，仍查 IP 白名单与封禁），细节见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。生产已永久关闭 `/docs`、`/redoc`、`/openapi.json`。

## 生命周期挂载

FastAPI `lifespan` 嵌套启动：`health.lifespan()` → `persistence.lifespan()` → `ai.lifespan()`（health 最外、ai 最内；停机逆序）。单次 run 的起停不在 lifespan，由 `RunController.start_run`/`stop_run` 编排，见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## 代码来源

- `app/main.py`（注册顺序 / 中间件 / lifespan / 静态挂载）
- `app/routers/*.py`（各 router 前缀与归属）
- `app/utils/gateway.py`（GatewayMiddleware）
