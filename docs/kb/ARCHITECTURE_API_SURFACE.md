> 更新时间：2026-08-02
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
| `/task` | `routers/task.py` | 任务消息、告警历史查询，及大屏只读清单（`/task/live` 在线、`/task/history` 历史） |
| `/traceback` | `routers/traceback.py` | 告警证据 / VOD playlist / 时间轴溯源 |
| `/media` | `routers/media.py` | HMAC token 化媒体访问（段 / init.mp4） |
| `/health` | `routers/health.py` | 健康状态与监控统计 |
| `/lab-f3m8` | `routers/lab.py` | 送标导出 + Label Studio（含静态 UI） |
| `/admin-f3m8` | `routers/admin.py` | 运维 Admin（概览 / 客户端 / 指标，含静态 UI） |

## 大屏清单端点接线（`/task/live`、`/task/history`）

`routers/task.py` 挂两个只读清单端点，供大屏「点条目→出画面」，无外部输入依赖：

- 二者均为**同步 `def`**（非 `async`），FastAPI 丢线程池执行——磁盘扫描（history）与 DB 查询不堵事件循环。
- `/task/live` 迭代 `client_manager.snapshot()`（COW 不可变 dict，迭代无需加锁）出在线 run。
- `/task/history` 无查询参数，两阶段避免每请求全盘扫段：`SegmentFinder.list_task_ids_by_recency()` 粗筛（mtime 近似序）→ 剔除活跃 task → 逐个 `list_steps()` 深扫、收满 `_HISTORY_LIMIT=10` 即停（`_HISTORY_SCAN_CAP=30` 兜住空目录病态）→ 按真实段 ts 重排 → 仅对最终 10 条查一次 DB 取 `source_ip`，整体 `try/except` 吞 DB 故障降级 `source_ip=null`（与 `/traceback/task/{id}/timeline` 同策略，不 503）。

清单只出参数、不出播放 URL；对外请求/响应契约见对外 API 文档（[docs/api/task.md](../api/task.md)）。段枚举能力归 `SegmentFinder`，见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md)。

唯一的 WebSocket 路由是 `/ai/video`；其余均为 HTTP。`/lab-f3m8/ui`、`/admin-f3m8/ui` 为 `StaticFiles` 挂载。

## 注册与中间件顺序

`app/main.py` 按序 `include_router`：api → health → ai → task → traceback → media → lab → admin（api 优先注册）。

中间件：`GatewayMiddleware` 在 CORS 之后 `add_middleware`；Starlette 逆序包装，故 **Gateway 最先执行**——所有 HTTP/WS 进路由前先过 IP 白名单 / 限流 / 反扫描。`/media` 默认走放宽策略（绕过限流与反扫描，仍查 IP 白名单与封禁），细节见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。生产已永久关闭 `/docs`、`/redoc`、`/openapi.json`。

## 生命周期挂载

FastAPI `lifespan` 嵌套启动：`health.lifespan()` → `persistence.lifespan()` → `ai.lifespan()`（health 最外、ai 最内；停机逆序）。persistence 平级于 inference，**须先于 inference 起、后于 inference 停**——以承接 `inference.stop()` 的结算告警 + HLS 残段 flush 后再抽干队列（顺序不可换，见 `app/main.py` lifespan 注释）。`yield` 返回即置 `app.state.shutdown_event`，通知 WebSocket 先退出，避免「WS 等 shutdown_event ↔ 清理等 WS」死锁。单次 run 的起停不在 lifespan，由 `RunController.start_run`/`stop_run` 编排，见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## 装配层解耦原则

服务实例在装配时**不得跨服务反向 push 私有字段**（反例：装配时伸手改另一服务的私有 `db_dir`，会在对方重构时静默崩）。存储根 `storage_base_dir` 由 `app/settings.py` 单一真源（`CLEANSIGHT_STORAGE_DIR`，相对路径以项目根解析），persistence / inference / traceback **三方一律读它**，不互相灌值。stage 路由为恒等（主键即 `step_id`，可读名下沉为 stage 的 `alias` 字段），无 `step→stage` 映射常量。改造背景见变更记录 `docs/update/20260627_INFRA_ASSEMBLY_DECOUPLE.md`。

## 代码来源

- `app/main.py`（注册顺序 / 中间件 / lifespan / 静态挂载）
- `app/routers/*.py`（各 router 前缀与归属）
- `app/utils/gateway.py`（GatewayMiddleware）
