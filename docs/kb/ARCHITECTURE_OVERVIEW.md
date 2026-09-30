> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 整体架构

CleanSight Backend 是一个 FastAPI 主进程加若干外部组件的实时视频 AI 系统。运行键全链路统一 int `task_id`。

`app/` 的子包依赖单向向下（`routers` → `daemons` → `services` → `services/utils` → `storage` / `db` →
`types`），层间边界由导入门禁测试锁死，细则见
[ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)。

## 主进程组件

- FastAPI 应用：`app/main.py`
- API Gateway 中间件：`app/gateway.py`（与 `mediamtx_gateway` 进程共用白名单 / 限流存储）
- 路由层：`app/routers/`；routers 共用的 run 解析与媒体 token 在 `app/routers/utils/`
- 流服务：`app/services/stream/`（仅 RTSP）
- 客户端状态：`app/services/client/`（COW 注册表 + per-run 不可变 CQ；单例 `client_service`）
- 运行编排：`app/services/run_control/`（`RunControlService` 跨服务起停单一出口，单例 `run_control_service`）
- 推理服务：`app/services/inference/`（`online/`：L1 检测 / L3 时序判定 / 可视化的实时链路；`offline/`：离线作业服务与全序列分割；两段共用 `config` / `stage_factory` / `resample`）
- 录制服务：`app/services/recording/`（**HLS 与检测结果落盘编排**：从活跃 CQ 拉整段、按提交序写、代次校验；见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)）
- 告警服务：`app/services/alarm/`（只做告警上报队列与重试；见 [SERVICE_ALARM.md](SERVICE_ALARM.md)）
- Lab 送标：`app/services/lab/`（无活体，模块函数）、`app/routers/lab.py`
- 算法服务：`app/services/algorithm/`（无状态纯计算，零 `app.*` 依赖，无单例 / 无 lifespan；当前只有试纸比色；见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md)）
- 追溯与媒体访问：`app/routers/traceback.py`、`app/routers/media.py`、`app/routers/utils/{runs,media_token}.py`（无对应 service 包）
- 后台 daemons：`app/daemons/health_monitor/`（断流重连 / 超时清理，拆除委托 `RunControlService`；见 [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)）、`app/daemons/cleanup/`（存储 TTL 清理，只依赖 `app.storage`；见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)）
- 服务层工具：`app/services/utils/`（多个 service 共用、不属于任一个：`task_queue` / `worker_guard` / `pressure` / `metrics` / `vod_playlist` VOD 清单渲染 / `media_timeline` 断流判定；媒体轴本身在 `storage.hls`）
- **数据层**：`app/storage/`（与 `services/` 平级、在它下面一层。内存模型 ↔ 盘上字节，按资源域分包：`hls/` / `inference/`，另有 `runs.py`、`tasks.py`、`utils/`；见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)）
- **平台 DB**：`app/db/`（与 `app/storage/` 平级的只读层：`database` 连接池 + `tasks` / `alarms` 一张表一个模块，ORM 映射 + `query_*`；只有 routers 调用）
- 共享契约：`app/types/`（frame / detection / temporal / alarm / run / exceptions）

## 外部组件

- MediaMTX：接收或转发 RTSP 流，`mediamtx/mediamtx.yml` 配置端口。
- FFmpeg：后端通过子进程读取流并解码 rawvideo。
- Postgres：只读，经 `app/db/` 的查询函数访问（SQLAlchemy），映射 `clean_task`（`app/db/tasks.py::DBTask`）、`clean_alarm`（`app/db/alarms.py::DBAlarm`）。
- 外部告警接口：`settings.alarm_report_url`。
- Label Studio：Lab 模块通过 HTTP API 上传裁剪视频。

## 生命周期

`app/main.py` 的 lifespan 逐层嵌套，**顺序即依赖**：

```text
health_monitor → stream → (cleanup, alarm) → recording → inference
```

`inference` 在最里层，因为它 `stop()` 时才交出最后一批结算告警与 HLS 残段——那一刻
alarm 的告警队列与 recording 的落盘队列必须**还活着**；等它交完，外层的 `finally`
才逐层停队列并抽干。嵌反了会让队列先停、残段提交被拒，而那些帧已经从 CQ 弹出去了，是真丢。

`cleanup` 与 `alarm` 写在同一个 `async with` 里、互不依赖：cleanup 在外，故起于告警池之前、停于
告警池之后；`enable_cleanup` 为假时 cleanup 不起线程。`inference.lifespan()` 内部先起在线
`inference_service`、后起 `offline_job_service`，停机时离线先停。health_monitor 最外层，全程看着
下面几层。algorithm 无活体，不在 lifespan 里。

单次 run 的起停不在 lifespan，由 `RunControlService.start_run`/`stop_run` 经 per-task 锁编排
（见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)）。服务单例构造一律不起线程、活儿推迟到
`start()`；构造期是否读 yaml 因包而异，见 [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md) §3。

## 路由注册顺序

`app/main.py` 按序注册 api、health、ai、task、traceback、media、lab、admin、algorithm 九个 router，
之后以单一挂载 `/ui-f3m8` 提供 admin / lab 两页与共用 vendor 前端库。

GatewayMiddleware 注册在 CORS 之后。Starlette 逆序包装，因此 Gateway 最先执行。

## 代码来源

- `app/main.py`
- `app/gateway.py`
- `app/routers/__init__.py`
- `app/services/run_control/service.py`
- `app/daemons/`
- `app/db/database.py`
- `mediamtx_gateway/main.py`
