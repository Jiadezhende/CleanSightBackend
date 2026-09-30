> 更新时间：2026-09-30
> 依据来源：代码分析（`app/types/exceptions.py` + `app/services/utils/worker_guard.py` + `app/services/alarm/alarm_worker.py` + `app/main.py` 异常处理器 + 调用点统计）
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 容错设计

容错分布在四个边界层、异常体系、兜底边界、健康监控和生命周期清理中。

## 四个边界层：业务代码只抛异常，捕获只发生在这四处

| 层 | 实现 | 覆盖什么 | 现状 |
|----|------|---------|------|
| L1 线程 | `guarded_run`（`app/services/utils/worker_guard.py`） | worker 主循环崩溃 → 冷却 2s 重启，连续 3 次仍崩则 critical 后放弃 | `app/` 内 4 处：`AlarmWorkerPool`、`StageAwareDispatcher`、`ClientTemporalActor`、`VisualizationWorkerPool` |
| L2 函数 | 告警上报重试 `_report_with_retry`（`app/services/alarm/alarm_worker.py`） | 单次上报调用的瞬时故障 → 有限次退避重试 | 唯一调用点 `AlarmWorker._process`；无通用重试框架 |
| L3 HTTP | `app/main.py` 的 10 个 `@app.exception_handler` | 异常 → HTTP 响应 | 状态码映射是**对外契约**，在 [docs/api/README.md](../api/README.md)，此处不复述 |
| L4 进程 | `app/main.py::main()` | 启动期配置/依赖错误 → fail-fast 退出 | — |

L1 与 L2 互补、不可互换：L2 重试的是**一次函数调用**（本仓只有告警上报），`guarded_run` 重启的是**整个 while 主循环**。后者覆盖主循环控制逻辑的意外异常与 C 扩展抛上来的 Python 级异常；**不覆盖** C 级 segfault 与死锁——那是 Service 级故障，要人工介入。

业务代码不用任何装饰器（重试、日志类都没有）。重试只在确有瞬时故障的调用点就地实现、判据只读自家异常的 `retryable` / `fatal`，不建通用重试框架——没有第二个调用点的框架只会让「导出了」被误当「在用」。

## 异常边界

自定义异常在 `app/types/exceptions.py`，共 8 个，全部继承 `AppError`。**两个标志决定重试层怎么处理它**（类属性，部分子类允许构造时实例级覆盖）：

| 异常 | retryable | fatal | 为什么 |
|------|:---------:|:-----:|--------|
| `StreamConnectionError` | ✅ | — | 网络瞬时故障；单路流失败不影响系统 |
| `DatabaseError` | ✅ | — | 连接池耗尽等可恢复；DB 故障不影响推理主流程 |
| `PersistenceError` | ✅ | — | 盘临时满 / 上报接口瞬时失败等可恢复（告警上报失败即抛它） |
| `FFmpegError` | — | ✅ | 二进制缺失 / 进程异常退出，重试修不好，须重启流 |
| `ModelInferenceError` | — | — | 语义：CUDA OOM 重试无用、单路失败不影响其他路；**现无 raise 点**（见下「Worker 边界」） |
| `NotFoundError` / `ValidationError` / `ConflictError` | — | — | 客户端错误，纯 HTTP 语义，重试无意义 |
| `AppError`（基类） | — | — | 默认值 |

`DatabaseError` / `ModelInferenceError` / `PersistenceError` 三个的构造函数收 `retryable=` 参数，可按调用点覆盖类属性；其余固定。

所有异常都带**运行坐标** `task_id` / `step_id`（排障主键）与辅助的 `source_ip`，`__str__` 会把它们连同 `[retryable]` / `[FATAL]` 标记一并拼进消息。

（丢帧不走异常：由 `frame_drop_total` 指标在真实丢帧点计数——见 [DESIGN_CONCURRENCY_AND_QUEUES.md](DESIGN_CONCURRENCY_AND_QUEUES.md) 背压部分。）

## 兜底只兜运行时推理失败

兜底（降级继续跑）的适用面要窄，判据是「失败发生在哪一层、有没有可用性要求」：

- **运行时推理失败 → 逐帧降级**：权重异常、CUDA 报错等只让该模型本批产出「失败的空结果」，画面照常（无框），不切换到别的实现。本仓：`stage_worker` 逐模型捕获，产 `success=False`、`boxes=[]` 的 `DetectorOutput`。
- **降级产物不得冒充真实结果落盘**：落盘格式若区分不了「失败」与「没检出」，失败帧就不落。本仓：`detections.jsonl` 不带 `success`，任一源失败的帧不入落盘缓冲（`DetectionService._write_back_results` 跳过 `append_ca_detections`）；失败时段在离线侧是时间空洞，整 run 全失败则离线 `skipped`。
- **参数错误在服务边界 400，不路由到兜底实现**：调用方给了系统没配置的东西，是请求错，不是运行时故障。本仓：`/api/start` 的 step 未在 YAML 定义或无 detector → `InferenceService.resolve_stage` 抛 `ValidationError`，且在锁外、动旧 run 之前校验（参数错不会先把旧 run 停掉）；离线提交 step 未配离线模型 → `require_offline` 400。
- **配置错误启动即失败**：构造失败时不静默少组件。本仓：`StageFactory` 任一 detector / operator 导入或构造失败即抛，`InferenceService` 包成 `RuntimeError` 冒到 lifespan，后端起不来；没配 detector 的 stage 只是不生效。
- **无可用性要求的路径不兜底**：失败即失败，让人看见。本仓：离线 Runner 未配置 → `ValidationError`，算法异常上抛，作业 `failed`；CLEAN 离线策略缺 `model_path` 直接 `ValueError`，不做规则降级。
- **反例**：兜底 stage 把未知 step 静默跑成透传——配置错误与「真打错 step」混在一起，谁都看不见；兜底实现产出的空结果还会被当成真实数据落盘、被下游消费。

## Worker 边界（L1）

推理侧 worker 不走 `AppError` 体系：它们捕获裸 `Exception`、打日志后继续，自愈靠进程监督与 L1。

- **推理子进程内**（`online/detection/stage_worker.py`）：逐模型 `infer_batch` 异常 → error 日志 + 该模型本批每帧产 `success=False` 的 `DetectorOutput`（逐帧降级，见上节）；collector 按 `error_type` 计 `infer_failure_total{model, error_type}`。
- **推理子进程死亡 / wedge / 久不就绪**（`infer_proxy.py::RemoteInferProxy`）：停止接收提交、kill 子进程、清孤儿在途帧（计 `frame_drop_total{reason="infer_child_restart"}`），指数退避重 spawn；超过最大重启次数 critical 后放弃。
- **主循环**（dispatcher 调度轮、Actor tick、可视化 tick、告警 worker）：循环体内 `except Exception` 打 error 后进入下一轮；外层再包 `guarded_run` 兜住循环控制逻辑本身的崩溃。
- `ModelInferenceError` 在 `app/` 内**零 raise 点**：只剩 `app/main.py` 的 L3 handler（HTTP 映射）与 `app/services/utils/metrics.py` docstring 里的示例；推理失败的可见性来自日志与 `infer_failure_total`，不来自这个异常类。

线程入口普遍通过 `guarded_run()` 包装，避免 worker 静默死亡。**两处刻意例外**，都写在各自的
docstring 里：`SerialTaskQueue._run` 不包（`_execute` 已吞掉所有任务异常，`_run` 自身只有
`queue.get`，包了也不可达，留着只会让人以为这里有自愈）；录制落盘不重试（见下）。

## 告警上报重试（L2）

`AlarmWorker._process` 经 `_report_with_retry` 调 `AlarmReporter.report_alarm()`（HTTP POST，失败抛 `PersistenceError`）。策略写死在 `alarm_worker.py`，零配置文件：

- 最多 3 次（含首次）；第 n 次失败后等 `min(1.0 × 2^(n-1), 30.0)` 秒 → 1s、2s。
- 判据 `_should_retry`：`fatal=True` → 直接上抛；`retryable=True` 且未满次数 → 重试；其余 → 上抛。**非 `AppError` 的异常一律 critical 后上抛，从不重试**——重试只认自家异常体系里的 `retryable` 标志，不猜第三方异常的性质。
- 每次异常强制打指标 `retry_total{operation="persistence", error_type=类名}`，重试后成功记 `error_type="recovered"`。标签值 `persistence` 是 Prometheus 对外契约，沿用不改。
- 重试耗尽 / 不可重试时 `_process` 记 `Report failed after retries`，worker 继续处理后续任务；停机时 worker 先把队列剩余任务处理完再退出。

### 录制落盘刻意不重试

录制落盘（`recording/service.py::_write` 写 HLS 段、`_write_detections` 追加检测结果）**不重试**，
异常由 `SerialTaskQueue._execute` 统一记 error 后吞掉。这不是疏漏，判据可迁移：

- **非幂等写不重试**：检测结果是纯追加，重试会写出重复帧；
- **失败非瞬时不重试**：现在会抛的失败（ffmpeg 缺失、盘满、权限、run 目录已被回收）重试也修不好；
- **丢小的保大的**：丢一段 ≈ 丢 10 秒录像、丢一批 ≈ 丢一个 sweep tick 的检测结果，比冒险写坏整个 run 的清单或数据便宜。

同理，写入所属 run 目录已被回收（`FileNotFoundError`）时的动作是**丢弃而不是重建目录或重试**——那个 run 已经不存在了，替它补写没有任何读者。

## 流连接容错

FFmpeg 启动失败时区分：

- transient stream unavailable：抛 `StreamConnectionError`。
- FFmpeg 二进制不存在或进程异常退出：抛 `FFmpegError`。

StreamService 初始启动失败时 decoder 仍注册，健康监控下一轮可感知并进入重连流程。

## 健康监控容错

断流（decoder 进程退出）后进入重连模式，按 `reconnect_interval` 节流反复 respawn；重连中无帧时长 ≥ `cleanup_timeout` 时经 `cleanup_client` 委托 `RunControlService.stop_run` 拆除该 run，避免半活状态持续占用资源（纯时间判据，不数重连次数）。

孤儿流和孤儿 decoder 会被定期检测并清理。细节见 [SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)。

## 优雅关闭

FastAPI lifespan finally 会先设置 `shutdown_event`，让 WebSocket handler 尽快退出，避免 WebSocket 等 shutdown、shutdown 等 WebSocket 的死锁。

之后按 lifespan 嵌套逆序停（内层先停）：

1. 离线作业服务先停：kill 在跑的离线子进程，排队作业记 cancelled（不与在线收尾抢 CPU）。
2. 在线推理服务：停模型推理服务 → signal 所有 TemporalActor 停止 → join 并 finalize，结算告警经 `alarm_sink` 入告警队列 → 停可视化池。
3. recording：先停 sweeper（不再拉新产物），再停两条落盘队列（排空已入队任务）。顺序反了会在队列停后继续拉，产物已从 CQ 弹出、提交被拒，是真丢。
4. 告警池（抽干队列）与 TTL 清理线程。
5. stream、health_monitor 最后。

⚠ 已知缺口：进程停机**不经** per-run 拆除（`RunControlService.stop_run`），不调 `recording.flush_residual`——各 run 的 CQ 里不足一段的残帧（≤ 约 10 s 录像）与最后约 1 s 的检测结果不落盘。`app/main.py` / recording 注释称停机会交出残段，与代码不符。可迁移的教训：「外层队列比内层写者活得久」只保证**已提交**的任务不丢，还在缓冲里、要靠拆除路径显式交出的东西，停机路径必须自己走一遍交出动作。

## 代码来源

- `app/types/exceptions.py`（8 个异常与 retryable/fatal 矩阵）
- `app/services/alarm/alarm_worker.py`（`_report_with_retry` / `_should_retry` / `_retry_delay`、指标副作用）
- `app/services/utils/worker_guard.py`（`max_restarts=3` / `cooldown=2.0`）
- `app/main.py`（10 个 `@app.exception_handler`、`main()` fail-fast、lifespan 嵌套）
- `app/services/inference/__init__.py`（离线作业服务先停）、`app/services/inference/online/service.py`（`stop`、`resolve_stage`、构造失败包 `RuntimeError`）
- `app/services/inference/online/detection/{stage_worker,infer_proxy,service}.py`（逐帧降级、子进程监督、降级帧不落盘）
- `app/services/inference/stage_factory.py`、`app/services/inference/config.py`（`require_offline`）
- `app/services/recording/service.py`（`_write` / `_write_detections` 不重试、`stop` 顺序）
- `app/services/stream/decoder.py`
- `app/daemons/health_monitor/worker.py`
- `tests/test_exception_handling.py`（告警上报重试与 `retry_total`）、`tests/test_boundary_layers.py`（6 个 L3 handler 的 HTTP 映射）
- `tests/test_inference_stage_routing.py`、`tests/test_start_rollback.py`、`tests/test_writeback_handle_fence.py`（兜底边界）
