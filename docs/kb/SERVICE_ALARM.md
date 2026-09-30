> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Alarm Service

告警服务（`app/services/alarm/`）只做一件事：把告警排队、异步 HTTP 上报到外部告警接口，失败按策略重试。它是**无状态上报层**，过闸、去重、mode 归属都不在这里。存储 TTL 清理是独立 daemon `app/daemons/cleanup/`（见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)）。

包内：`service.py`（`AlarmService`：队列 + 池，对外 `persist_alarm()`）、`alarm_worker.py`（`AlarmWorkerPool` / `AlarmWorker` + `_report_with_retry`）、`reporter.py`（`AlarmReporter` 单次 HTTP）、`types.py`（`AlarmReportTask`）、`config.py`、`instance.py`（单例 `alarm_service`）；`__init__.py` 只有 `lifespan()`。

## 生命周期：alarm 在 inference 外层，停机结算告警不丢

`lifespan()` 起 `alarm_service.start()`，`finally` 里 `stop(timeout=10.0)`。它与 cleanup 同层、嵌在 inference 外层：停机时 `run_control` 逐个 `stop_run` 交出的结算告警落进仍在跑的队列，之后 alarm 再停池、把剩余任务处理完（完整停机顺序见 [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)）。

单例构造就会读配置：`AlarmService(config=None)` 调 `get_alarm_config()` 读 yaml（进程内缓存），建 `alarm_queue` 与 `AlarmWorkerPool` 对象，但不起线程；线程在 `start()` 里起。

## AlarmService：纯入队，不去重

- `persist_alarm(alarm_info: Dict) -> bool`：`AlarmReportTask.from_dict` 后 `put(timeout=0.5)`；`queue.Full` 或其他异常都记日志返回 False，不抛。
- `AlarmReportTask.timestamp` 取入队时刻，`to_dict()` 时序列化为 `detected_at`。

## AlarmWorker：逐条上报，停机前处理完队列

`AlarmWorkerPool.start()` 为每个 worker（默认 1 个）起一条 daemon 线程，线程体经 `guarded_run`（线程级自愈）包 `AlarmWorker.run`：`get(timeout=0.5)` 轮询、逐条 `_process`；停止信号后先处理完队列剩余任务再退出。`stop(timeout)` 置停止信号并逐线程 join。

`_process` 调 `_report_with_retry(lambda: reporter.report_alarm(d))`；重试耗尽或不可重试时记 `Report failed after retries` 并丢弃该条。`_report_with_retry` 的语义：

| 情形 | 处理 |
|------|------|
| 首次或重试后成功 | 返回；经过重试的记 `retry_total{error_type="recovered"}` |
| `AppError`，非 `fatal`、`retryable` 且已尝试 < 3 次 | WARNING `Retry n/3`，sleep 后重试 |
| `AppError`，`fatal` / 不可重试 / 已满 3 次 | ERROR（`Fatal error`），上抛 |
| 非 `AppError` | CRITICAL，上抛，不重试 |

- 最多 3 次（含首次），退避 `min(1.0 × 2^(n-1), 30.0)` 即 1 s、2 s；常量写死在模块顶部，不走配置。
- 每次异常记 `retry_total{operation="persistence", error_type=<异常类名>}`。`operation="persistence"` 是 Prometheus 对外契约，不改。
- 这是 `app/` 内唯一的函数级重试实现，不是通用框架。

## AlarmReporter：单次 HTTP 上报

- 只在 `task_id` 为真且 `step_id` 不为 None 时上报，否则直接返回。
- `urllib` POST JSON 到 `settings.alarm_report_url`，超时 10 s；payload 含 `task_id` / `step_id` / `step_name`（取 `stage`）/ `alarm_type` / `alarm_level` / `alarm_message` / `alarm_time`（`detected_at` 格式化为 `%Y-%m-%d %H:%M:%S`），有 `detection_result` 时附上。
- 响应 JSON `code == 0` 才算成功；HTTP 异常、非 JSON、`code != 0` 都判失败。

> ⚠ **上报失败实际不会重试（代码缺陷）**：失败分支构造 `PersistenceError(message=…, client_id=…, operation=…, retryable=True)`，而 `PersistenceError.__init__` 不接受 `client_id`（`app/types/exceptions.py`），于是抛出的是 `TypeError`。它走上表「非 `AppError`」一行：CRITICAL、不重试、`retry_total{error_type="TypeError"}`。现有 `tests/test_exception_handling.py` 直接构造 `PersistenceError`，覆盖不到这条路径。

## 过闸与编排归 inference 域

过闸去重（5 s 冷却）、mode 归属、别名烧录都在 inference 侧：`ClientQueues.append_alarm_record_with_gate` 按 `(metric, mode)` 管冷却窗口与前端环形日志；`inference/online/temporal/alarm_sink.persist_alarms` 编排「过闸 → 入日志 → `alarm_service.persist_alarm`」。调用方：temporal actor（实时）、`RunControlService.stop_run`（结算，含停机）、`InferenceService.stop()`（停机兜底，正常为空）。告警服务只读 `alarm_info` 里定好的字段。

依赖方向只许 inference → alarm：`alarm_sink` 是单例引用面的具名例外，`test_alarm_does_not_import_inference` 锁死反向。

## 配置

`config/persistence_config.yaml` 的 `alarm:` 段 → `AlarmServiceConfig`：`workers: 1`、`queue_size: 200`。同文件 `storage:` 段归 cleanup。文件缺失、解析失败或 `alarm:` 段出现未知字段，都整体退回默认值（记日志）。

## 代码来源

- `app/services/alarm/{__init__,service,instance,alarm_worker,reporter,config,types}.py`
- `app/types/exceptions.py`（`PersistenceError` 构造签名）
- `app/services/inference/online/temporal/alarm_sink.py`、`app/services/client/queues.py`（`append_alarm_record_with_gate`）
- `app/settings.py`（`alarm_report_url`）、`config/persistence_config.yaml`
- `tests/test_exception_handling.py`（`_should_retry` / `_retry_delay` / `_report_with_retry` + `retry_total`）、`tests/test_alarm_sink.py`、`tests/test_alarm_increment.py`
