> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# API 路由接线图

本文件描述 HTTP/WS 层的**架构接线**：有哪些 router、各自前缀与归属、往下调谁、注册与中间件顺序、生命周期挂载。**各端点的请求/响应契约、字段语义与调用示例属对外 API 文档范围（[docs/api/](../api/README.md)），不在本知识库维护**（本库只回答「有哪些路由、谁拥有、怎么接线」）。

运行键为 int `task_id`；业务端点（`/api`、`/ai`）对 `task_id`（首选）与旧 `client_id`(=source_ip) 双模兼容，对外 wire 未变。

## Router 归属

| 前缀 | router | 职责（一句） |
|------|--------|------------|
| `/api` | `routers/api.py` | 统一任务入口：启动/终止一次 run（`db_tasks.query_task` 取行后桥接 `run_control_service.start_run`） |
| `/ai` | `routers/ai.py` | 实时推理 WebSocket `/ai/video`（渲染帧推送）+ `POST /ai/temporal`（读某 run 的离线分割段，换算到媒体刻度） |
| `/task` | `routers/task.py` | 任务消息、告警历史查询，及大屏只读清单（`/task/live` 在线、`/task/history` 历史） |
| `/traceback` | `routers/traceback.py` | run 级 VOD playlist + 时间轴溯源（两个端点，必填 `step_id`，可选 `run_id`，缺省最新可见 run） |
| `/media` | `routers/media.py` | HMAC token 化媒体访问（段 / `{track}_init.mp4`），token 锁定 run |
| `/health` | `routers/health.py` | 健康状态与监控统计（只读 `health_monitor_worker`） |
| `/lab-f3m8` | `routers/lab.py` | 送标导出 + Label Studio；`POST /label-probs`（离线模型逐帧类别概率旁路）；`GET /tasks` 行带 `offline_steps` |
| `/admin-f3m8` | `routers/admin.py` | 运维 Admin（概览 / 客户端 / 指标）+ 离线推理作业（提交 / 列表 / 单个状态），经单例 `offline_job_service` |
| `/algorithm` | `routers/algorithm.py` | 无状态算法（试纸比色 `POST /algorithm/colorstrip`），只转调 `services/algorithm/service`，见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md) |

唯一的 WebSocket 路由是 `/ai/video`；其余均为 HTTP。

静态资产只有一个挂载 `/ui-f3m8`（`StaticFiles(directory=app/static, html=True)`，目录由 `__file__` 推导、不依赖 CWD）：`/ui-f3m8/admin/`、`/ui-f3m8/lab/` 两页 + `/ui-f3m8/vendor/` 共用前端库；根 `/ui-f3m8/` 返回 404、不列目录。页面入口与 API 前缀分离：API 仍在 `/admin-f3m8`、`/lab-f3m8`。

## router → 下层依赖

routers 只做装配：检查顺序、HTTP 错误映射、DTO 组装。**不持有 DB session、不内联 ORM 查询**——平台 DB 一律经 `app/db` 的 `query_*`（每次调用自开自关 session，失败抛 `DatabaseError`，降级策略由 router 定）；盘上产物经 `app/storage` 的 `query_*` / `list_*` / `read_*`。`app/db/database.py::get_db` 仍定义但全仓无调用方，没有任何 router 用 `Depends(get_db)`。这条「router 不自开 session、不拼存储原语算业务量」是现状，**无门禁**（`test_import_hygiene` 没有 routers 白名单）。

| router | 平台 DB（`app/db`） | 存储（`app/storage`） | 服务 / routers.utils |
|---|---|---|---|
| `api.py` | `db_tasks.query_task`（无行 404、`source_ip` 空 400） | — | `run_control_service.start_run`（`asyncio.to_thread`，起流期间不持 DB 连接） |
| `task.py` | `db_alarms.query_task_alarms`；`db_tasks.query_source_ips`（history 补点位，任何异常降级 `source_ip=null`） | `runs.query_latest_by_step` + `hls.query_span`（`_summarise_steps`）；`tasks.list_task_ids(order="recent")` / `tasks.latest_run_id`（history 粗排） | `client_service.snapshot()` |
| `traceback.py` | `db_alarms.query_step_alarms` + `detected_at_ms`（`DatabaseError` 退化为空 events） | `hls.list_segments` / `query_has_init`（playlist）；`hls.query_span` / `query_timeline` + `runs.query_lifespan_ms`（timeline） | `resolve_run`、`MediaToken`、`services/utils` 的 `total_gap_ms` / `render_vod` |
| `ai.py` | — | `inference.read_temporal` | `resolve_timeline`、`client_service` |
| `lab.py` | `db_tasks.query_task_page`（db 模式任务列表） | `runs.query_latest_by_step` + `hls.query_has_segments` / `query_span`、`inference.query_has_offline_results`、`tasks.list_task_ids`（storage 模式） | `services/lab/service`（送标 / 导出 / 探活流程）、`lab/runtime_config`、`resolve_run` / `resolve_timeline` |
| `media.py` | — | `hls` 的路径解析 | `resolve_media_run`、`MediaToken` |
| `admin.py` | — | — | `offline_job_service`、`client_service`、`resolve_run` / `no_run` |
| `health.py` | — | — | `health_monitor_worker`（routers → daemons 只读） |
| `algorithm.py` | — | — | `services/algorithm/service`（base64 解码与两个具名异常 → 400 留在 router） |

跨 router 共用的 HTTP 侧工具在 `app/routers/utils/`：

- `runs.py`：读侧入口**解析一次 run**，往下只传 `RunIdentity`。`resolve_run`（点名的 run 不在 → 404 Run；缺省且无可见 run → None，由端点维持原「该 step 没数据」响应）、`no_run`（必须有 run 的端点在 None 分支抛的 404）、`resolve_timeline`（`resolve_run` + `hls.query_timeline`，无段 → 404 Segments；`/ai/temporal`、`/lab-f3m8/label-probs` 用）、`resolve_media_run`（`/media/*` 用，一律 404 "Media file not found"）。两类解析响应形态不同，刻意不合并。
- `media_token.py`：`MediaToken`（payload 带可选 `"r": run_id`），只被 `traceback` / `media` 用。

## 离线作业接线

`routers/admin.py` 直接持 `offline_job_service` 单例。提交路径（同步 `def`）：`offline_job_service.require_offline(step_id)`（未配置 400）→ `resolve_run` / `no_run`（404）→ `submit(run)`（该 run 仍是当前注册 CQ 的 run / 队满 → 409；同一 run 在途则返回在途作业）。顺序即「参数 400 先于 404 先于 409」。作业不在请求线程里跑：入作业服务的串行队列、由 CLI 子进程执行（细节见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)）。列表端点出在途 + 最近结束作业；单个状态端点缺省 `run_id` 时解析最新可见 run，无作业 404。离线结果的读口是 `/ai/temporal`（分段）与 `/lab-f3m8/label-probs`（逐帧概率），二者都经 `resolve_timeline` 锁定同一 run 的媒体轴。

## 大屏清单端点接线（`/task/live`、`/task/history`）

`routers/task.py` 挂两个只读清单端点，供大屏「点条目→出画面」，无外部输入依赖：

- 二者均为**同步 `def`**（非 `async`），FastAPI 丢线程池执行——磁盘扫描（history）与 DB 查询不堵事件循环。
- `/task/live` 迭代 `client_service.snapshot()`（COW 不可变 dict，迭代无需加锁）出在线 run。
- `/task/history` 无查询参数，两阶段避免每请求全盘扫段：`tasks.list_task_ids(order="recent")`（按 `tasks.latest_run_id` 粗排）→ 剔除活跃 task → 逐个深扫 `_summarise_steps`（`runs.query_latest_by_step` 取各 step 最新可见 run → `hls.query_span`，None 即丢该 step）→ 终排键 `max(steps[].run_id)`；收满 `_HISTORY_LIMIT=10` 后，下一候选的上界 `latest_run_id` 已低于第 10 名的排序键即停（精确截断，`_HISTORY_SCAN_CAP=30` 兜住空目录病态）→ 仅对最终 10 条调一次 `db_tasks.query_source_ips`，任何异常降级 `source_ip=null`（与 `/traceback/task/{id}/timeline` 同策略，不 503）。
- ⚠ `runs.query_latest_by_step` **只看 run 可见、不看有没有段**。「两轨都没段就丢弃」必须由 `_summarise_steps` 按 `query_span is None` 自己补，否则清单会把起流即失败的 step 露给前端点开黑屏。
- 粗排键只用于剪枝，**绝不当对外时间戳**——对外时间取 `HlsSpan` 的毫秒值，`latest_ms` 只作展示；粗筛与深扫之间任务可能刚起/刚停，清单短暂不一致由下一轮轮询自愈，不加锁。

清单只出参数（含 `steps[].run_id`）、不出播放 URL；对外请求/响应契约见 [docs/api/task.md](../api/task.md)。段枚举与跨度能力归 `app.storage.hls` / `app.storage.runs` / `app.storage.tasks`，见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md) 与 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)。

`POST /algorithm/colorstrip` 同样是同步 `def`：整个函数体都在阻塞 CPU（实测最慢样本约 156 ms，来源 20260920 记录，待核验），放在事件循环上会卡住同进程的 `/ai/video` WS。

## 注册与中间件顺序

`app/main.py` 按序 `include_router`：api → health → ai → task → traceback → media → lab → admin → algorithm（api 优先注册），之后 `mount("/ui-f3m8")`。

中间件：`GatewayMiddleware` 在 CORS 之后 `add_middleware`；Starlette 逆序包装，故 **Gateway 最先执行**——所有 HTTP/WS 进路由前先过 IP 白名单 / 限流 / 反扫描。`/media` 走 bypass（绕过限流与反扫描，仍查 IP 白名单与封禁）；`/ui-f3m8` 静态页与 `/algorithm` 不在任何 relaxed / bypass 前缀里，走普通档，细节见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。生产已永久关闭 `/docs`、`/redoc`、`/openapi.json`。

## 生命周期挂载

FastAPI `lifespan` 嵌套启动（`app/main.py`，起序 = 嵌套顺序，停序逆序）：

```text
health_monitor → stream → (cleanup, alarm) → recording → inference
                                                          └ 内部：inference_service 先起后停，offline_job_service 后起先停
```

- `health_monitor`（`app/daemons/health_monitor/`）最外层：最先起、最后停，全程看着下面几层。
- `cleanup`（`app/daemons/cleanup/`）与 `alarm` 同一个 `async with`：cleanup 在外层，先于告警池起、后于告警池停；二者互不依赖。
- alarm、recording 都在 inference 外层（**先于 inference 起、后于 inference 停**）：`inference.stop()` 会 finalize 所有 Actor、把结算告警经 `alarm_sink` 入 alarm 队列，alarm 的 finally 再抽干；recording 队列比写者活得久，sweeper 停机前已拉走的段与检测结果照常落盘。
- ⚠ 已知缺口：进程停机路径**不经** `RunControlService.stop_run`（全仓只有 `/api` 与 health_monitor 调它），因此不调 `recording.flush_residual`——各 cq 里不足一段的残帧（≤ 一个段长，约 10 s 录像）与最后约 1 s 的检测结果**不落盘**。`app/main.py` lifespan 注释与 recording 注释称停机会交出残段，与代码不符。
- `inference.lifespan()` 内部停机时离线作业服务**先停**（kill 在跑的离线子进程，不与在线收尾抢 CPU），再停在线 `inference_service`。
- algorithm 无活体，不在 lifespan 里。
- `yield` 返回即置 `app.state.shutdown_event`，通知 WebSocket 先退出，避免「WS 等 shutdown_event ↔ 清理等 WS」死锁。

单次 run 的起停不在 lifespan，由 `RunControlService.start_run` / `stop_run` 编排，见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

## 装配层解耦原则

服务实例在装配时**不得跨服务反向 push 私有字段**（反例：装配时伸手改另一服务的私有 `db_dir`，会在对方重构时静默崩）。存储根 `storage_base_dir` 由 `app/settings.py` 单一真源（`CLEANSIGHT_STORAGE_DIR`，相对路径以项目根解析），cleanup / recording / inference / routers **一律读它**（经 settings 或 `app.storage` 的根解析），不互相灌值。stage 路由为恒等（主键即 `step_id`，可读名下沉为 stage 的 `alias` 字段），无 `step→stage` 映射常量。

## 代码来源

- `app/main.py`（注册顺序 / 中间件 / lifespan / 静态挂载）
- `app/routers/*.py`（各 router 前缀与归属、下层依赖）
- `app/routers/utils/{runs,media_token}.py`
- `app/db/{database,tasks,alarms}.py`（`query_*`；`get_db` 无调用方）
- `app/services/inference/__init__.py`（在线 / 离线作业服务的起停顺序）
- `app/gateway.py`（GatewayMiddleware）、`app/settings.py`（`gateway_relaxed_prefixes` / `gateway_bypass_prefixes`）
- `tests/test_static_mount.py`、`tests/test_router_utils_runs.py`、`tests/test_admin_offline_jobs.py`
