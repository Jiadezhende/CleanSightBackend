> 更新时间：2026-09-22
> 依据来源：代码分析（`app/utils/` 全量 + `app/main.py` 异常处理器 + 调用点统计）
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 容错设计

容错分布在四个边界层、异常体系、健康监控和生命周期清理中。

## 四个边界层：业务代码只抛异常，捕获只发生在这四处

| 层 | 实现 | 覆盖什么 | 现状 |
|----|------|---------|------|
| L1 线程 | `guarded_run`（`app/utils/worker_guard.py`） | worker 主循环崩溃 → 冷却 2s 重启，连续 3 次仍崩则 critical 后放弃 | `app/` 内 5 处 |
| L2 函数 | `GuardedExecutor.execute`（`app/utils/executor.py`） | 单次调用的瞬时故障 → 按 policy 重试 | `app/` 内 8 处 |
| L3 HTTP | `app/main.py` 的 10 个 `@app.exception_handler` | 异常 → HTTP 响应 | 状态码映射是**对外契约**，在 [docs/api/README.md](../api/README.md)，此处不复述 |
| L4 进程 | `app/main.py::main()` | 启动期配置/依赖错误 → fail-fast 退出 | — |

L1 与 L2 互补、不可互换：`GuardedExecutor` 重试的是**一次函数调用**（如落盘、上报），`guarded_run` 重启的是**整个 while 主循环**。后者覆盖主循环控制逻辑的意外异常与 C 扩展抛上来的 Python 级异常；**不覆盖** C 级 segfault 与死锁——那是 Service 级故障，要人工介入。

业务代码不写 `try/except`、不用 `@retry` 类装饰器；允许的装饰器只有日志用途的 `log_call`（`CLEANSIGHT_DEBUG` 为真时才激活，见 `app/utils/decorators.py`）。

## 异常边界

自定义异常在 `app/utils/exceptions.py`，共 8 个，全部继承 `AppError`。**两个标志决定 L2 怎么处理它**（类属性，部分子类允许构造时实例级覆盖）：

| 异常 | retryable | fatal | 为什么 |
|------|:---------:|:-----:|--------|
| `StreamConnectionError` | ✅ | — | 网络瞬时故障；单路流失败不影响系统 |
| `DatabaseError` | ✅ | — | 连接池耗尽等可恢复；DB 故障不影响推理主流程 |
| `PersistenceError` | ✅ | — | 盘临时满等可恢复 |
| `FFmpegError` | — | ✅ | 二进制缺失 / 进程异常退出，重试修不好，须重启流 |
| `ModelInferenceError` | — | — | CUDA OOM 重试无用；但单路失败不影响其他路，故**不**致命 |
| `NotFoundError` / `ValidationError` / `ConflictError` | — | — | 客户端错误，纯 HTTP 语义，重试无意义 |
| `AppError`（基类） | — | — | 默认值 |

`DatabaseError` / `ModelInferenceError` / `PersistenceError` 三个的构造函数收 `retryable=` 参数，可按调用点覆盖类属性；其余固定。

所有异常都带**运行坐标** `task_id` / `step_id`（排障主键）与辅助的 `source_ip`，`__str__` 会把它们连同 `[retryable]` / `[FATAL]` 标记一并拼进消息。

（丢帧不走异常：由 `frame_drop_total` 指标在真实丢帧点计数——见 [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md) 背压部分。）

## Worker 边界（L1）

推理 worker 的边界层处理：

- `ModelInferenceError`：error 后继续；CUDA/OOM 会记录指标。
- `AppError`：error 后继续。
- 未预期异常：critical 后继续。

线程入口普遍通过 `guarded_run()` 包装，避免 worker 静默死亡。**两处刻意例外**，都写在各自的
docstring 里：`SerialTaskQueue._run` 不包（`_execute` 已吞掉所有任务异常，`_run` 自身只有
`queue.get`，包了也不可达，留着只会让人以为这里有自愈）；录制落盘不包（见下）。

## GuardedExecutor（L2）

五个预定义 policy，硬编码在 `GuardedExecutor.POLICIES`，零配置文件：

| policy | 最大尝试 | 首次延迟 | 退避 |
|--------|:-------:|:-------:|------|
| `stream` | 5 | 3.0s | 固定 |
| `database` | 3 | 1.0s | ×2，封顶 60s |
| `external_api` | 3 | 2.0s | ×2，封顶 60s |
| `inference` | 2 | 1.0s | 固定 |
| `persistence` | 3 | 1.0s | ×2，封顶 30s |

`execute(func, policy_name=...)` 的决策只有三条：`fatal=True` → 直接上抛；`retryable=True` 且未超次数 → 重试；其余 → 上抛。**非 `AppError` 的异常一律 critical 后上抛，从不重试**——重试只认自家异常体系里的 `retryable` 标志，不猜第三方异常的性质。

每次决策都强制打指标：`retry_total{operation, error_type}`（重试与失败都计，成功恢复记 `error_type="recovered"`），`ModelInferenceError.is_cuda_error` 为真时另计 `gpu_oom_total{model}`。

Alarm worker 用 `persistence` policy 执行上报；重试失败后记录错误，worker 继续处理后续任务。

需要自定义策略时传 `custom_policies={...}`（`ExecutionPolicy` 数据类），**不要在业务代码里手写重试循环**。

### HLS 落盘刻意不重试

录制落盘（`recording/service.py::_write`）**不包 `GuardedExecutor`、失败不重试**，异常由
`SerialTaskQueue._execute` 统一记 error 后吞掉。这不是疏漏：

- `insert_segment` 把清单条目排在最后登记，重试若落在「条目已追加、统计写失败」之后，会往
  playlist 里写出**重复 EXTINF**，毁掉整个 step 的回放；
- 而现在会抛的失败（ffmpeg 缺失/换代、盘满）基本都是**非瞬时**的，重试也修不好。

丢一段 ≈ 丢 10 秒录像，比毁一整段回放便宜。同理，代次校验判定「这段属于上一代」时的动作是
**丢弃而不是重读重试**——旧 run 的段在新 run 里没有任何意义。看见「乐观锁」三个字别顺手补一个
重试循环。

## 已导出但当前无调用点

`app/utils/__init__.py` 的 `__all__` 比实际用到的大一圈。扩容前先确认下表，别把「导出了」当成「在用」：

| 符号 | `app/` 内调用点 | 说明 |
|------|:--------------:|------|
| `CircuitBreaker` | 0 | 仅 `tests/test_boundary_layers.py` 覆盖。打开时抛裸 `Exception`，接它要注意类型 |
| `RetryExecutorWithCircuitBreaker` | 0 | 同上。**注意名字**：旧文档里写作 `GuardedExecutorWithCircuitBreaker`，那个符号从未存在过 |
| `timing` | 0 | 仅 `tests/test_decorators.py` 覆盖 |
| `is_retryable_error` / `is_fatal_error` | 0 | 全仓库零引用——`GuardedExecutor` 直接读 `exc.fatal` / `exc.retryable`，没走这两个函数 |
| `log_call` | 1 | 唯一在用的日志装饰器 |

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

- `app/utils/exceptions.py`（8 个异常与 retryable/fatal 矩阵）
- `app/utils/executor.py`（`POLICIES` 表、`_decide_action`、指标副作用）
- `app/utils/worker_guard.py`（`max_restarts=3` / `cooldown=2.0`）
- `app/utils/decorators.py`（`log_call` / `timing`，`CLEANSIGHT_DEBUG` 条件激活）
- `app/main.py`（10 个 `@app.exception_handler`、`main()` fail-fast）
- `app/services/stream/decoder.py`
- `app/services/health_monitor/monitor.py`
- `app/services/inference/detection/service.py`
- `tests/test_exception_handling.py`、`tests/test_boundary_layers.py`
