> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 整体架构

CleanSight Backend 是一个 FastAPI 主进程加若干外部组件的实时视频 AI 系统。运行键全链路统一 int `task_id`。

## 主进程组件

- FastAPI 应用：`app/main.py`
- API Gateway 中间件：`app/utils/gateway.py`
- 路由层：`app/routers/`
- 流服务：`app/services/stream/`（仅 RTSP）
- 客户端状态：`app/services/client/`（COW 注册表 + per-run 不可变 CQ）
- 运行编排：`app/services/run_control.py`（`RunController` 跨服务起停单一出口）
- 推理服务：`app/services/inference/`（L1 检测 / L2 特征 / L3-L4 时序判定 / 可视化；online/offline 分离，离线消费端待实现）
- 持久化服务：`app/services/persistence/`（无状态落库，HLS PULL）
- 健康监控：`app/services/health_monitor/`（重连/清理委托 RunController）
- 追溯与媒体访问：`app/services/traceback/`、`app/routers/traceback.py`、`app/routers/media.py`
- Lab 送标：`app/services/lab/`、`app/routers/lab.py`
- 共享契约：`app/domain/`（frame/detection/alarm/render）

## 外部组件

- MediaMTX：接收或转发 RTSP 流，`mediamtx/mediamtx.yml` 配置端口。
- FFmpeg：后端通过子进程读取流并解码 rawvideo。
- Postgres：应用 ORM 使用 SQLAlchemy 连接，模型映射 `clean_task`、`clean_alarm`。
- 外部告警接口：`settings.alarm_report_url`。
- Label Studio：Lab 模块通过 HTTP API 上传裁剪视频。

## 生命周期

FastAPI lifespan 中按顺序启动：健康监控路由生命周期 → AI 推理服务生命周期。AI 生命周期启动 `InferenceManager`，停止时关闭推理、流、可视化和持久化相关线程。单次 run 的起停不在 lifespan，由 `RunController.start_run`/`stop_run` 经 per-task 锁编排（见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)）。服务实例化的饿汉/惰性分层见 [SERVICE_CONFIG.md](SERVICE_CONFIG.md)。

## 路由注册顺序

`app/main.py` 注册了统一 API、health、ai、task、traceback、media、lab、admin，并挂载了 admin/lab 静态 UI。

GatewayMiddleware 注册在 CORS 之后。Starlette 逆序包装，因此 Gateway 最先执行。

## 代码来源

- `app/main.py`
- `app/routers/__init__.py`
- `app/services/run_control.py`
- `app/database.py`
- `mediamtx_gateway/main.py`

