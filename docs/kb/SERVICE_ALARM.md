> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Alarm Service

告警服务（`app/services/alarm/`）**只做一件事**：把告警排队、异步 HTTP 上报到外部告警接口，失败按
策略重试。它是**无状态上报层**——过闸 / 去重 / 模式归属编排都不在此。

存储 TTL 清理不在本服务，是独立的 daemon `app/daemons/cleanup/`，见
[ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md) 的 TTL 段。HLS 落盘也不在本服务：
写侧是 `app/services/recording/`（见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)）。

## 包结构与生命周期

```text
app/services/alarm/
  __init__.py       门面型：docstring + lifespan()，零 re-export
  service.py        AlarmService：告警队列 + AlarmWorkerPool，对外 persist_alarm()
  instance.py       单例 alarm_service
  alarm_worker.py   AlarmWorkerPool / AlarmWorker 线程体 + 上报重试 _report_with_retry
  reporter.py       AlarmReporter：单次 HTTP 上报（无状态）
  config.py         AlarmServiceConfig / get_alarm_config()：读 persistence_config.yaml 的 alarm: 段
  types.py          AlarmReportTask：队列里的上报任务形状
```

`lifespan()` 起 `alarm_service.start()`、`finally` 里 `stop(timeout=10.0)`。在 `app/main.py` 里与
cleanup 同层、嵌在 inference 外层（`health_monitor → stream → (cleanup, alarm) → recording → inference`）：
`inference.stop()` 交出的结算告警落进仍在跑的告警队列，之后再停池抽干——保序、不丢尾。

单例构造会读配置：`AlarmService(config=None)` 在构造函数里调 `get_alarm_config()` 读 yaml 并打加载日志
（进程内缓存）；构造建 `alarm_queue` 与 `AlarmWorkerPool` 对象，但不起线程。线程在 `start()` 里起。

## AlarmService：纯入队

- `start()` / `stop(timeout)` 只起停告警池（`AlarmWorkerPool`，`workers` 个线程，默认 1）。
- `persist_alarm(alarm_info: Dict) -> bool`：`AlarmReportTask.from_dict` 后 `put(timeout=0.5)`；
  `queue.Full` 或其它异常都记日志返回 `False`，不抛。**无过闸 / 去重**。
- `AlarmReportTask.timestamp` 取入队时刻（`time.time()`），序列化为 `detected_at` 交给 reporter。

## AlarmWorker 与上报重试

`AlarmWorkerPool.start()` 每个 worker 起一条 daemon 线程，线程体经 `guarded_run`（线程级自愈，
`app/services/utils/worker_guard.py`）包 `AlarmWorker.run`：`get(timeout=0.5)` 轮询队列，逐条
`_process`；停止信号后**把队列剩余任务处理完**再退出。`stop(timeout)` 置停止信号并逐线程 `join`。

`_process` 调 `_report_with_retry(lambda: reporter.report_alarm(d))`，重试耗尽或不可重试时记
`Report failed after retries` 并丢弃该条（不回队）。`_report_with_retry` 语义（`alarm_worker.py`）：

| 情形 | 处理 |
|---|---|
| 首次或重试后成功 | 返回；若经过重试，`retry_total{error_type="recovered"}` +1 |
| `AppError`，非 `fatal`、`retryable` 且尝试次数 < 3 | WARNING `[AlarmWorker] Retry n/3 after Xs`，sleep 后重试 |
| `AppError`，`fatal` / 不可重试 / 已满 3 次 | ERROR + exc_info（文案 `Fatal error`），原样上抛 |
| 非 `AppError` | CRITICAL + exc_info，上抛，不重试 |

- 最多 3 次（含首次）；退避 `min(1.0 × 2^(n-1), 30.0)` 秒，即 1s、2s。常量写死在模块顶部，不走配置。
- 每次异常 `retry_total{operation="persistence", error_type=<异常类名>}` +1。`operation` 标签值沿用旧名，
  是 Prometheus 对外契约，不改。
- 这是 `app/` 内唯一的函数级重试实现，不是通用框架（容错分层见
  [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)）。

## AlarmReporter：单次 HTTP 上报

- 只在 `task_id` 为真且 `step_id` 不为 None 时上报；否则直接返回（不上报、不报错）。
- `urllib` POST JSON 到 `settings.alarm_report_url`，超时 10s；payload 含 `task_id` / `step_id` /
  `step_name`（取 `stage`）/ `alarm_type` / `alarm_level` / `alarm_message` / `alarm_time`（`detected_at`
  格式化为 `%Y-%m-%d %H:%M:%S`）及可选 `detection_result`。
- 响应 JSON `code == 0` 才算成功；HTTP 异常、非 JSON、`code != 0` 一律抛
  `PersistenceError(retryable=True)`，交给上面的重试。

## 过闸与编排归属 inference 侧

**过闸去重（5s 冷却）+ mode 归属 + 别名烧录在 inference 侧**：`ClientQueues.append_alarm_record_with_gate`
（`app/services/client/queues.py`）按 `(metric, mode)` 管冷却窗口与前端轮询的告警环形缓冲；
`app/services/inference/online/temporal/alarm_sink.persist_alarms` 编排「过闸 → 入缓冲 → `alarm_service.persist_alarm`」，
调用方是 temporal actor（实时）、`InferenceService` 与 `RunControlService`（结算）。告警服务只读
`alarm_info` 里已定好的字段上报。

> 边界固定：告警过闸 / 编排归属 inference 域，**不迁入 alarm**。方向只许 inference → alarm：
> `alarm_sink` 是单例引用面的具名例外，`test_alarm_does_not_import_inference` 锁死反向。

## 配置

`config/persistence_config.yaml` 的 `alarm:` 段 → `AlarmServiceConfig`：`workers: 1`、`queue_size: 200`。
同文件的 `storage:` 段归 cleanup（`app/daemons/cleanup/config.py`）。文件缺失或解析失败时整体退默认值；
`alarm:` 段出现未知字段即构造失败、同样退默认值。

## 代码来源

- `app/services/alarm/{__init__,service,instance,alarm_worker,reporter,config,types}.py`
- `app/services/inference/online/temporal/alarm_sink.py`（过闸编排归属）
- `app/services/client/queues.py`（`append_alarm_record_with_gate`）
- `app/settings.py`（`alarm_report_url`）
- `config/persistence_config.yaml`（`alarm:` 段）
- `tests/test_exception_handling.py`（`_should_retry` / `_retry_delay` / `_report_with_retry` + `retry_total`）、`tests/test_alarm_sink.py`、`tests/test_alarm_increment.py`（闸门）
