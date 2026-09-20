> 更新时间：2026-09-20
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

- `ModelInferenceError`：error 后继续；CUDA/OOM 会记录指标。
- `AppError`：error 后继续。
- 未预期异常：critical 后继续。

（丢帧不走异常：由 `frame_drop_total` 指标在真实丢帧点计数——见 [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md) 背压部分。）

线程入口普遍通过 `guarded_run()` 包装，避免 worker 静默死亡。**两处刻意例外**，都写在各自的
docstring 里：`SerialTaskQueue._run` 不包（`_execute` 已吞掉所有任务异常，`_run` 自身只有
`queue.get`，包了也不可达，留着只会让人以为这里有自愈）；录制落盘不包（见下）。

## GuardedExecutor

Alarm worker 使用 `GuardedExecutor` 的 `persistence` policy 执行上报。重试失败后记录错误，但
worker 继续处理后续任务。

### HLS 落盘刻意不重试

录制落盘（`recording/service.py::_write`）**不包 `GuardedExecutor`、失败不重试**，异常由
`SerialTaskQueue._execute` 统一记 error 后吞掉。这不是疏漏：

- `insert_segment` 把清单条目排在最后登记，重试若落在「条目已追加、统计写失败」之后，会往
  playlist 里写出**重复 EXTINF**，毁掉整个 step 的回放；
- 而现在会抛的失败（ffmpeg 缺失/换代、盘满）基本都是**非瞬时**的，重试也修不好。

丢一段 ≈ 丢 10 秒录像，比毁一整段回放便宜。同理，代次校验判定「这段属于上一代」时的动作是
**丢弃而不是重读重试**——旧 run 的段在新 run 里没有任何意义。看见「乐观锁」三个字别顺手补一个
重试循环。

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
- `app/services/inference/detection/service.py`
- `tests/test_exception_handling.py`

