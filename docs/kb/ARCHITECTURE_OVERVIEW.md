> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 整体架构

CleanSight Backend 是一个 FastAPI 主进程加若干外部组件的实时视频 AI 系统。

## 主进程组件

- FastAPI 应用：`app/main.py`
- API Gateway 中间件：`app/utils/gateway.py`
- 路由层：`app/routers/`
- 流服务：`app/services/stream/`
- 客户端状态：`app/services/client/`
- 推理服务：`app/services/inference/`
- 持久化服务：`app/services/persistence/`
- 健康监控：`app/services/health_monitor/`
- 追溯与媒体访问：`app/services/traceback/`、`app/routers/traceback.py`、`app/routers/media.py`
- Lab 送标：`app/services/lab/`、`app/routers/lab.py`

## 外部组件

- MediaMTX：接收或转发 RTSP/RTMP 流，`mediamtx/mediamtx.yml` 配置 1935、8004 等端口。
- FFmpeg：后端通过子进程读取流并解码 rawvideo。
- Postgres：应用 ORM 使用 SQLAlchemy 连接，模型映射 `clean_task`、`clean_alarm`。
- 外部告警接口：`settings.alarm_report_url`。
- Label Studio：Lab 模块通过 HTTP API 上传裁剪视频。

## 生命周期

FastAPI lifespan 中按顺序启动：

1. 健康监控路由生命周期。
2. AI 推理服务生命周期。

AI 生命周期启动 `InferenceManager`，停止时会关闭推理、流、可视化和持久化相关线程。

## 路由注册顺序

`app/main.py` 注册了统一 API、health、ai、task、traceback、media、lab、admin，并挂载了 admin/lab 静态 UI。

GatewayMiddleware 注册在 CORS 之后。Starlette 逆序包装，因此 Gateway 最先执行。

## 代码来源

- `app/main.py`
- `app/routers/__init__.py`
- `app/services/ai.py`
- `app/database.py`
- `mediamtx_gateway/main.py`

