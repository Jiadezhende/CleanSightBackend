> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 整体架构

CleanSight Backend 是一个 FastAPI 主进程加若干外部组件的实时视频 AI 系统。运行键全链路统一 int `task_id`。

代码分五层、依赖单向向下（`routers` → `services` → `services/utils` → `storage` / `utils` →
`domain`），层间边界由导入门禁测试锁死，细则见
[ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)。

## 主进程组件

- FastAPI 应用：`app/main.py`
- API Gateway 中间件：`app/utils/gateway.py`
- 路由层：`app/routers/`
- 流服务：`app/services/stream/`（仅 RTSP）
- 客户端状态：`app/services/client/`（COW 注册表 + per-run 不可变 CQ）
- 运行编排：`app/services/run_control.py`（`RunController` 跨服务起停单一出口）
- 推理服务：`app/services/inference/`（L1 检测 / L2 特征 / L3-L4 时序判定 / 可视化；online/offline 分离）
- 录制服务：`app/services/recording/`（**HLS 落盘编排**：从活跃 CQ 拉整段、按提交序写、代次校验；见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)）
- 持久化服务：`app/services/persistence/`（**现只剩告警上报 + TTL 回收**，HLS 已移交 recording）
- 健康监控：`app/services/health_monitor/`（重连/清理委托 RunController）
- 追溯与媒体访问：`app/services/traceback/`、`app/routers/traceback.py`、`app/routers/media.py`
- Lab 送标：`app/services/lab/`、`app/routers/lab.py`
- 服务层工具：`app/services/utils/`（无状态纯函数，多个 service 共用、不属于任一个：`media_timeline` 媒体轴换算、`vod_playlist` VOD 清单渲染）
- **数据层**：`app/storage/`（与 `services/` 平级、在它下面一层。内存模型 ↔ 盘上字节，按资源域分包：`hls/` / `feature.py` / `tasks.py`；见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)）
- 共享契约：`app/domain/`（frame/detection/alarm/render）

## 外部组件

- MediaMTX：接收或转发 RTSP 流，`mediamtx/mediamtx.yml` 配置端口。
- FFmpeg：后端通过子进程读取流并解码 rawvideo。
- Postgres：应用 ORM 使用 SQLAlchemy 连接，模型映射 `clean_task`、`clean_alarm`。
- 外部告警接口：`settings.alarm_report_url`。
- Label Studio：Lab 模块通过 HTTP API 上传裁剪视频。

## 生命周期

`app/main.py` 的 lifespan 逐层嵌套，**顺序即依赖**：

```text
health_monitor → stream → persistence → recording → inference
```

`inference` 在最里层，因为它 `stop()` 时才交出最后一批结算告警与 HLS 残段——那一刻
persistence 的告警队列与 recording 的落盘队列必须**还活着**；等它交完，外层的 `finally`
才逐层停队列并抽干。嵌反了会让队列先停、残段提交被拒，而那些帧已经从 CQ 弹出去了，是真丢。

单次 run 的起停不在 lifespan，由 `RunController.start_run`/`stop_run` 经 per-task 锁编排
（见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)）。服务单例一律「只挂名不干活」，构造
零副作用、活儿推迟到 `start()`，见 [ARCHITECTURE_PACKAGE_LAYERS.md](ARCHITECTURE_PACKAGE_LAYERS.md)。

## 路由注册顺序

`app/main.py` 注册了统一 API、health、ai、task、traceback、media、lab、admin，并挂载了 admin/lab 静态 UI。

GatewayMiddleware 注册在 CORS 之后。Starlette 逆序包装，因此 Gateway 最先执行。

## 代码来源

- `app/main.py`
- `app/routers/__init__.py`
- `app/services/run_control.py`
- `app/database.py`
- `mediamtx_gateway/main.py`

