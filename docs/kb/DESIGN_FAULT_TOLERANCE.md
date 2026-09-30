> 更新时间：2026-09-30
> 依据来源：代码分析（`app/types/exceptions.py` + `app/services/utils/worker_guard.py` + `app/services/alarm/alarm_worker.py` + `app/main.py` 异常处理器 + 调用点统计）
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 容错设计

## 四个边界层：业务代码只抛异常，捕获只发生在这四处

| 层 | 实现 | 覆盖什么 | 现状 |
|----|------|---------|------|
| L1 线程 | `guarded_run`（`app/services/utils/worker_guard.py`） | worker 主循环崩溃 → 冷却 2 s 重启，连续 3 次仍崩则 critical 后放弃 | 4 处：`AlarmWorkerPool`、`StageAwareDispatcher`、`ClientTemporalActor`、`VisualizationWorkerPool` |
| L2 函数 | `_report_with_retry`（`app/services/alarm/alarm_worker.py`） | 单次调用的瞬时故障 → 有限次退避重试 | 唯一调用点 `AlarmWorker._process` |
| L3 HTTP | `app/main.py` 的 10 个 `@app.exception_handler` | 异常 → HTTP 响应 | 状态码映射是对外契约，见 [docs/api/README.md](../api/README.md) |
| L4 进程 | `app/main.py::main()` | 启动期配置 / 依赖错误 → fail-fast 退出 | — |

- **L1 与 L2 不可互换**：L2 重试一次函数调用，L1 重启整个 while 主循环。L1 覆盖循环控制逻辑的意外异常与 C 扩展抛上来的 Python 级异常，**不覆盖** segfault 与死锁（那是服务级故障，要人介入）。
- **不建通用重试框架、不用装饰器**：重试只在确有瞬时故障的调用点就地实现，判据只读自家异常的 `retryable` / `fatal`。没有第二个调用点的框架只会让「导出了」被误当「在用」。
- **刻意不包 L1 的地方写进 docstring**：`SerialTaskQueue._run` 不包 `guarded_run`——`_execute` 已吞掉所有任务异常，`_run` 只剩 `queue.get`，包了也不可达，只会让人以为这里有自愈。

## 异常体系：retryable / fatal 两个标志决定重试层怎么处理

`app/types/exceptions.py` 共 8 个异常，全部继承 `AppError`：

| 异常 | retryable | fatal | 语义 |
|------|:---------:|:-----:|------|
| `StreamConnectionError` | ✅ | — | 网络瞬时故障；单路失败不影响系统 |
| `DatabaseError` | ✅ | — | 连接池耗尽等可恢复 |
| `PersistenceError` | ✅ | — | 盘临时满、上报接口瞬时失败等可恢复 |
| `FFmpegError` | — | ✅ | 二进制缺失 / 进程异常退出，重试修不好 |
| `ModelInferenceError` | — | — | `app/` 内零 raise 点，只剩 L3 handler 与 `metrics.py` docstring 示例 |
| `NotFoundError` / `ValidationError` / `ConflictError` | — | — | 客户端错误，纯 HTTP 语义 |

- 两个标志是类属性；基类 `AppError` 经 `**kwargs` 允许实例级覆盖，`DatabaseError` / `ModelInferenceError` / `PersistenceError` 的构造函数显式收 `retryable=`。
- 所有异常都带运行坐标 `task_id` / `step_id`（排障主键）与辅助的 `source_ip`，`__str__` 连同 `[retryable]` / `[FATAL]` 标记拼进消息。**各子类构造函数的关键字参数是封闭的**，传错名字（如 `client_id`）会变成 `TypeError`，绕过整个重试判据——告警上报正踩在这里，见 [SERVICE_ALARM.md](SERVICE_ALARM.md)。
- 丢帧不走异常，由 `frame_drop_total{reason}` 在真实丢帧点计数。

## 重试判据：只认自家异常的标志，非幂等写不重试

- **重试只认自家体系**：`fatal=True` 直接上抛；`retryable=True` 且未满次数才重试；非 `AppError` 一律 critical 后上抛、从不重试——不猜第三方异常的性质。本仓唯一实现是告警上报（3 次、1 s / 2 s 退避，详见 [SERVICE_ALARM.md](SERVICE_ALARM.md)）。
- **非幂等写不重试**：纯追加的写重试会写出重复记录；「先写数据、最后登记清单条目」的写，重试会写出重复条目、毁掉整个清单。
- **失败非瞬时不重试**：ffmpeg 缺失、盘满、权限、目标已被回收，重试也修不好。
- **丢小的保大的**：丢一段 ≈ 丢 10 s 录像、丢一批 ≈ 丢一个 sweep tick 的检测结果，都比冒险写坏整个 run 便宜。
- **写入目标已被回收时丢弃，不重建、不重试**：那一代已不存在，替它补写没有读者。
- 例证：录制落盘 `_write` / `_write_detections` 只调一次，异常由 `SerialTaskQueue._execute` 记 error 吞掉（[SERVICE_RECORDING.md](SERVICE_RECORDING.md)）。

## 兜底只兜运行时推理失败

判据：失败发生在哪一层、这条路径有没有可用性要求。

| 情形 | 原则 | 本仓落地 |
|------|------|----------|
| 运行时推理失败 | 逐帧降级：该模型本批产「失败的空结果」，画面照常，不切换实现 | `stage_worker` 逐模型捕获，产 `success=False`、`boxes=[]`；计 `infer_failure_total{model, error_type}` |
| 降级产物 | 格式区分不了「失败」与「没检出」时，失败帧不落盘 | `detections.jsonl` 不带 `success`，任一源失败的帧不进 `ca_detections`；离线侧见时间空洞，整 run 全失败则 `skipped` |
| 参数错误 | 服务边界 400，不路由到兜底实现 | `resolve_stage` 对未配置 step 抛 `ValidationError`，锁外、动旧 run 之前校验；离线 `require_offline` 400 |
| 配置错误 | 启动即失败，不静默少组件 | `StageFactory` 任一组件构造失败即抛，`InferenceService` 包成 `RuntimeError`，后端起不来 |
| 无可用性要求的路径 | 不兜底，失败让人看见 | 离线 Runner 未配置 → `ValidationError`；算法异常作业 `failed`；缺 `model_path` 直接 `ValueError` |

**反例**：兜底 stage 把未知 step 静默跑成透传——配置错误与「真打错 step」混在一起谁都看不见，兜底产出的空结果还会被当成真实数据落盘、被下游消费。

## 自愈靠进程监督与 L1，不靠异常类

推理侧 worker 不走 `AppError` 体系，捕获裸 `Exception`、打日志后继续：

- **推理子进程死亡 / wedge / 久不就绪**（`RemoteInferProxy`）：停止接收提交、kill 子进程、清孤儿在途帧（计 `frame_drop_total{reason="infer_child_restart"}`），指数退避重 spawn，超过最大重启次数 critical 后放弃。
- **主循环**（dispatcher 调度轮、Actor tick、可视化 tick、告警 worker）：循环体内 `except Exception` 打 error 后进入下一轮，外层再包 `guarded_run` 兜住循环控制逻辑本身。
- **流与健康监控**：ffmpeg 秒退按 stderr 区分 `StreamConnectionError` 与 `FFmpegError`，首启失败 decoder 仍注册；断流重连、超时拆除、孤儿清理全由健康监控按时钟驱动（[SERVICE_STREAM.md](SERVICE_STREAM.md)、[SERVICE_HEALTH_MONITOR.md](SERVICE_HEALTH_MONITOR.md)）。

## 优雅关闭：先放 WebSocket，再从最内层逐个交出

- **先放 WebSocket**：`yield` 返回即置 `shutdown_event`，避免「WS 等 shutdown_event ↔ 清理等 WS」死锁。
- **产出方在内层先停，承接产出的队列在外层后停、排空**；离线作业先于在线推理停，不与在线收尾抢 CPU。
- **停机路径必须自己走一遍拆除交出动作**：「外层队列比内层写者活得久」只保证已提交的任务不丢，还在缓冲里、要靠拆除路径显式交出的东西（残段、最后一批检测结果、结算告警）不会自己进队列。本仓：`run_control.lifespan` 在最内层，停机先对每个 run 调 `stop_run`（[SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)）。
- 各层完整停机顺序见 [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)。

## 代码来源

- `app/types/exceptions.py`（8 个异常与 retryable / fatal）
- `app/services/alarm/{alarm_worker,reporter}.py`（`_report_with_retry` / `_should_retry` / `_retry_delay`）
- `app/services/utils/{worker_guard,task_queue}.py`
- `app/main.py`（10 个 exception handler、`main()` fail-fast、lifespan 嵌套）、`app/services/run_control/{__init__,service}.py`（停机拆 run）
- `app/services/inference/__init__.py`、`app/services/inference/online/service.py`（`stop`、`resolve_stage`、构造失败包 `RuntimeError`）
- `app/services/inference/online/detection/{stage_worker,infer_proxy,service}.py`、`app/services/inference/stage_factory.py`、`app/services/inference/config.py`（`require_offline`）
- `app/services/recording/service.py`、`app/services/stream/decoder.py`、`app/daemons/health_monitor/worker.py`
- `tests/test_exception_handling.py`、`tests/test_boundary_layers.py`、`tests/test_inference_stage_routing.py`、`tests/test_start_rollback.py`、`tests/test_writeback_handle_fence.py`、`tests/test_run_control_shutdown.py`
