> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 整体架构

CleanSight Backend 是一个 FastAPI 主进程加若干外部组件的实时视频 AI 系统，运行键全链路为 int `task_id`。
`app/` 子包依赖单向向下（`routers` → `daemons` → `services` → `services/utils` → `storage` / `db` → `types`），
由导入门禁测试锁死。

## 主进程组件

| 组件 | 位置 | 要点 |
|------|------|------|
| FastAPI 应用 | `app/main.py` | lifespan、路由注册、异常处理器、`GET /metrics` |
| API Gateway 中间件 | `app/gateway.py` | 与 `mediamtx_gateway` 进程共用白名单 / 限流 Store 实现，见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md) |
| 路由层 | `app/routers/`（+ `utils/`：run 解析、媒体 token） | 见 [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md) |
| 流服务 | `app/services/stream/` | 仅 RTSP；每 run 一个 FFmpeg decoder |
| 客户端状态 | `app/services/client/` | COW 注册表 + per-run 不可变 CQ；单例 `client_service` |
| 运行编排 | `app/services/run_control/` | 跨服务起停单一出口 `run_control_service`，见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md) |
| 推理 | `app/services/inference/` | `online/`：L1 检测 / L3 时序 / 可视化；`offline/`：离线作业与全序列分割，见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md) |
| 录制 | `app/services/recording/` | 从活跃 CQ 拉整段，落 HLS 与检测结果，见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md) |
| 告警 | `app/services/alarm/` | 只做上报队列与重试，见 [SERVICE_ALARM.md](SERVICE_ALARM.md) |
| Lab 送标 | `app/services/lab/` + `app/routers/lab.py` | 无活体，模块函数 |
| 算法 | `app/services/algorithm/` | 无状态纯计算，零 `app.*` 依赖，当前只有试纸比色，见 [SERVICE_ALGORITHM.md](SERVICE_ALGORITHM.md) |
| 追溯与媒体访问 | `app/routers/{traceback,media}.py` | 无对应 service 包，见 [SERVICE_TRACEBACK_MEDIA.md](SERVICE_TRACEBACK_MEDIA.md) |
| 健康监控 daemon | `app/daemons/health_monitor/` | 断流重连 / 超时清理，拆除委托 run_control，见 [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md) |
| 存储 TTL daemon | `app/daemons/cleanup/` | 只依赖 `app.storage`，见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md) |
| 数据层 | `app/storage/` | 内存模型 ↔ 盘上字节，见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md) |
| 平台 DB | `app/db/` | 只读 ORM + `query_*`；只有 routers 调用 |

`services/utils/`、`types/` 等基础包的成员与边界见 [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)。

## 外部组件

- **MediaMTX + RTSP 网关**：独立进程 `mediamtx_gateway`，只开 RTSP，见 [SERVICE_GATEWAY_MEDIAMTX.md](SERVICE_GATEWAY_MEDIAMTX.md)。
- **FFmpeg**：后端以子进程拉流并解码为 rawvideo；HLS 段转码也用它。
- **Postgres**：只读，经 `app/db/` 访问，映射 `clean_task`（`DBTask`）、`clean_alarm`（`DBAlarm`）。
- **外部告警接口**：`settings.alarm_report_url`。
- **Label Studio**：Lab 模块经 HTTP API 上传裁剪视频。

## lifespan 嵌套顺序即依赖：run_control 最内层，停机时先拆 run

`app/main.py` 逐层嵌套（起序 = 嵌套序，停序逆序）：

```text
health_monitor → stream → (cleanup, alarm) → recording → inference → run_control
                                                         └ 内部：inference_service 先起后停，offline_job_service 后起先停
```

停机顺序（内层先退）：

1. `yield` 返回即置 `app.state.shutdown_event`，让 WebSocket 先退出，避免「WS 等 shutdown ↔ 清理等 WS」死锁。
2. `run_control.lifespan` 退出：`RunControlService.shutdown()` 对 `client_service.snapshot()` 里每个 run 调
   `stop_run(task_id, "shutdown")`——停 decoder、`stop_workflow` 收结算告警并入 alarm 队列、
   `recording.flush_residual(cq)` 交出残段与剩余检测结果、注销 CQ。
3. `inference.lifespan` 退出：先停离线作业服务（kill 在跑的子进程），再 `inference_service.stop()`；actor 已被
   stop_run 摘空，其 Phase 2 结算只作兜底。
4. `recording` 停 sweeper 并排空队列（残段在此落盘）；`alarm` 抽干告警队列；随后 `cleanup`、`stream`
   （收掉不属于任何 run 的 decoder）、`health_monitor` 依次退出。

硬约束：拆 run 必须早于 `inference.stop()`，且 recording / alarm 队列此时仍活着——所以二者嵌在 inference 外层、
run_control 嵌在 inference 里层。嵌反会让残段提交被拒，而那些帧已从 CQ 弹出，是真丢。

其余：`cleanup` 与 `alarm` 同一个 `async with`、互不依赖，cleanup 在外；`enable_cleanup` 为假时 cleanup 不起线程。
algorithm、lab 无活体，不在 lifespan 里。单次 run 的起停不走 lifespan，由 `RunControlService.start_run` /
`stop_run` 经 per-task 锁编排。单例构造期是否读 yaml 见 [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)
「单例构造」一节。

路由注册与中间件顺序见 [ARCHITECTURE_API_SURFACE.md](ARCHITECTURE_API_SURFACE.md)。

## 代码来源

- `app/main.py`（lifespan 嵌套、`shutdown_event`、`/metrics`）
- `app/services/run_control/{__init__,service}.py`（`lifespan()` / `shutdown()` / `stop_run`）
- `app/services/inference/__init__.py`、`app/services/inference/online/service.py`（`stop()` Phase 2 兜底）
- `app/services/{recording,alarm,stream}/__init__.py`、`app/daemons/{cleanup,health_monitor}/__init__.py`
- `app/db/{database,tasks,alarms}.py`
- `tests/test_run_control_shutdown.py`
