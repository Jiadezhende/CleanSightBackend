> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# API 路由接线图

本文只回答「有哪些 router、谁拥有、往下调谁、怎么注册」。端点的请求 / 响应契约、字段语义与错误码在
[docs/api/](../api/README.md)，不在本库维护。

运行键为 int `task_id`；`/api/terminate` 与 WS `/ai/video` 另兼容旧 `client_id`（= source_ip），经
`client_service.find_by_source_ip` 解析。

## Router 归属

| 前缀 | router | 职责 |
|------|--------|------|
| `/api` | `routers/api.py` | 启动 / 终止一次 run |
| `/ai` | `routers/ai.py` | WS `/ai/video`（渲染帧）+ `POST /ai/temporal`（离线分段换算到媒体刻度） |
| `/task` | `routers/task.py` | 任务消息、告警历史、大屏清单 `/task/live` / `/task/history` |
| `/traceback` | `routers/traceback.py` | run 级 VOD playlist（GET + HEAD）+ 时间轴溯源 |
| `/media` | `routers/media.py` | HMAC token 化的段 / init 访问，token 锁定 run |
| `/health` | `routers/health.py` | 健康状态与监控统计（只读 `health_monitor_worker`） |
| `/lab-f3m8` | `routers/lab.py` | 送标导出 + Label Studio、`POST /label-probs`、`GET /tasks`（带 `offline_steps`）、运行时配置 |
| `/admin-f3m8` | `routers/admin.py` | 运维概览 / 客户端 / 指标 + 离线推理作业 |
| `/algorithm` | `routers/algorithm.py` | 试纸比色 `POST /algorithm/colorstrip`，见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md) |

router 之外：`GET /metrics`（Prometheus 文本）直接定义在 `app/main.py`；静态资产单一挂载 `/ui-f3m8`
（`StaticFiles(directory=app/static, html=True)`，目录由 `__file__` 推导）出 `/ui-f3m8/admin/`、`/ui-f3m8/lab/` 两页与
`/ui-f3m8/vendor/`，根 `/ui-f3m8/` 返回 404。页面 API 仍在 `/admin-f3m8`、`/lab-f3m8`。唯一的 WebSocket 路由是 `/ai/video`。

## router 只做装配，DB 与存储经下层查询函数

router 只留检查顺序、HTTP 错误映射与 DTO 组装：平台 DB 一律经 `app/db` 的 `query_*`（每次自开自关 session，失败抛
`DatabaseError`，降级由 router 定）；盘上产物经 `app/storage` 的 `query_*` / `list_*` / `read_*`。这一条没有门禁。

| router | 平台 DB（`app/db`） | 存储（`app/storage`） | 服务 / routers.utils |
|---|---|---|---|
| `api.py` | `db_tasks.query_task`（无行 404、`source_ip` 空 400） | — | `run_control_service.start_run` / `stop_run`（均 `asyncio.to_thread`）；terminate 经 `client_service.get` / `find_by_source_ip` 找 CQ |
| `task.py` | `db_alarms.query_task_alarms`；`db_tasks.query_source_ips`（history 补点位） | `runs.query_latest_by_step` + `hls.query_span`；`tasks.list_task_ids` / `latest_run_id` | `client_service`；`inference.online.naming.get_task_metric_map`（函数体内） |
| `traceback.py` | `db_alarms.query_step_alarms` + `detected_at_ms`（`DatabaseError` 退化为空 events） | `hls.list_segments` / `query_has_init`（playlist）；`hls.query_span` / `query_timeline` + `runs.query_lifespan_ms`（timeline） | `resolve_run`、`MediaToken`、`services/utils` 的 `total_gap_ms` / `render_vod` |
| `ai.py` | — | `inference.read_temporal` | `resolve_timeline`、`client_service` |
| `lab.py` | `db_tasks.query_task_page`（db 模式任务列表） | `runs` / `hls` / `tasks`（storage 模式）、`inference.query_has_offline_results` | `services/lab/{service,runtime_config,step_exporter}`、`resolve_run` / `resolve_timeline` |
| `media.py` | — | `hls` 路径解析 | `resolve_media_run`、`MediaToken` |
| `admin.py` | — | — | `offline_job_service`、`client_service`、`resolve_run` / `no_run` |
| `health.py` | — | — | `health_monitor_worker`（routers → daemons 只读） |
| `algorithm.py` | — | — | `services/algorithm/service`（base64 解码与两个具名异常 → 400 留在 router） |

`app/routers/utils/` 放 ≥2 个 router 共用的 HTTP 侧工具：

- `runs.py`：读侧入口解析一次 run，往下只传 `RunIdentity`。`resolve_run`（点名的 run 不在 → 404；缺省且无可见 run →
  None）、`no_run`（None 分支抛的 404）、`resolve_timeline`（`resolve_run` + `hls.query_timeline`，无段 404）、
  `resolve_media_run`（`/media/*` 专用，一律 404 "Media file not found"）。两类解析响应形态不同，刻意不合并。
- `media_token.py`：`MediaToken`（payload 带可选 `"r": run_id`），只被 `traceback` / `media` 用。

## 离线作业提交按「400 → 404 → 409」顺序校验

`routers/admin.py` 的 `POST /admin-f3m8/offline/jobs`（同步 `def`）：`offline_job_service.require_offline(step_id)`
（未配置 400）→ `resolve_run` / `no_run`（404）→ `submit(run)`（该 run 仍是当前注册 CQ 的 run 或队满 → 409；同一 run
在途则返回在途作业）。作业入串行队列、由 CLI 子进程执行，见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)。

## 大屏清单 `/task/history` 两阶段扫描，只对最终 10 条查 DB

- `/task/live`、`/task/history` 均为同步 `def`，FastAPI 丢线程池执行，磁盘扫描与 DB 查询不堵事件循环。
- `/task/live` 迭代 `client_service.snapshot()`（COW 不可变 dict，无需加锁）。
- `/task/history`：`tasks.list_task_ids(order="recent")` 按 `latest_run_id` 粗排 → 剔除活跃 task → 逐个
  `_summarise_steps` 深扫 → 终排键 `max(steps[].run_id)`。收满 `_HISTORY_LIMIT=10` 后，下一候选的 `latest_run_id`
  已低于第 10 名即停（`_HISTORY_SCAN_CAP=30` 兜空目录）。最后对这 10 条调一次 `db_tasks.query_source_ips`，任何异常
  降级 `source_ip=null`、不 503。
- ⚠ `runs.query_latest_by_step` 只看 run 可见、不看有没有段。「两轨都没段就丢该 step」由 `_summarise_steps` 按
  `hls.query_span is None` 自己补，否则清单会露出起流即失败的 step，前端点开黑屏。
- 粗排键只用于剪枝，不作对外时间；清单只出参数（含 `steps[].run_id`）、不出播放 URL。

`POST /algorithm/colorstrip` 也是同步 `def`：函数体整段阻塞 CPU（最慢样本约 156 ms，待核验），放在事件循环上会卡住
同进程的 `/ai/video`。

## 注册与中间件：Gateway 最先执行

- `include_router` 顺序：api → health → ai → task → traceback → media → lab → admin → algorithm，之后 `mount("/ui-f3m8")`。
- `GatewayMiddleware` 在 CORS 之后 `add_middleware`，Starlette 逆序包装，所以 Gateway 最先执行：HTTP 与 WS 握手进路由前
  先过 IP 白名单 / 限流 / 反扫描。分档（bypass / relaxed / normal）见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。
- `/docs`、`/redoc`、`/openapi.json` 在所有环境关闭（`FastAPI(docs_url=None, ...)`）。
- lifespan 挂载顺序与停机拆 run 见 [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)；`yield` 返回即置
  `app.state.shutdown_event`，WS handler 据此先退出。

## 代码来源

- `app/main.py`（注册顺序 / 中间件 / 静态挂载 / `/metrics`）
- `app/routers/*.py`、`app/routers/utils/{runs,media_token}.py`
- `app/db/{database,tasks,alarms}.py`
- `tests/test_static_mount.py`、`tests/test_router_utils_runs.py`、`tests/test_admin_offline_jobs.py`、`tests/test_task_live_history_api.py`
