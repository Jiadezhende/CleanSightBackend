> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 容错设计

容错分布在异常体系、GuardedExecutor、健康监控和生命周期清理中。

## 异常边界

自定义异常在 `app/utils/exceptions.py`：

- `StreamConnectionError`
- `FFmpegError`
- `DatabaseError`
- `ModelInferenceError`
- `PersistenceError`
- `NotFoundError`
- `ValidationError`
- `ConflictError`

FastAPI 全局异常处理器在 `app/main.py` 将这些异常转换为 HTTP 响应。

## Worker 边界

推理 worker 的边界层处理：

- `FrameDrop`：warning 后继续。
- `ModelInferenceError`：error 后继续；CUDA/OOM 会记录指标。
- `AppError`：error 后继续。
- 未预期异常：critical 后继续。

线程入口普遍通过 `guarded_run()` 包装，避免 worker 静默死亡。

## GuardedExecutor

HLS 和 Alarm worker 使用 `GuardedExecutor` 的 `persistence` policy 执行持久化。重试失败后记录错误，但 worker 继续处理后续任务。

## 流连接容错

FFmpeg 启动失败时区分：

- transient stream unavailable：抛 `StreamConnectionError`。
- FFmpeg 二进制不存在或进程异常退出：抛 `FFmpegError`。

StreamService 初始启动失败时 decoder 仍注册，健康监控下一轮可感知并进入重连流程。

## 健康监控容错

断流后进入重连模式。超过最大重连次数时统一清理 client，避免半活状态持续占用资源。

孤儿流和孤儿 decoder 会被定期检测并清理。

## 优雅关闭

FastAPI lifespan finally 会先设置 `shutdown_event`，让 WebSocket handler 尽快退出，避免 WebSocket 等 shutdown、shutdown 等 WebSocket 的死锁。

AI 停止流程：

1. 停止模型推理服务。
2. signal 所有 TemporalActor 停止。
3. finalize 收集结算告警。
4. 停止可视化池。
5. 停止持久化服务并等待队列。

## 代码来源

- `app/utils/exceptions.py`
- `app/utils/executor.py`
- `app/utils/worker_guard.py`
- `app/main.py`
- `app/services/stream/decoder.py`
- `app/services/health_monitor/monitor.py`
- `app/services/inference/core/service.py`
- `tests/test_exception_handling.py`

