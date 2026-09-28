# persistence 服务改名 alarm，GuardedExecutor 内联进 alarm_worker

> **变更状态**：已完成（2026-09-28）——搬迁与改名；告警重试从通用执行器内联进 alarm_worker，重试语义不变
> **知识库**：待沉淀

## 概述

`app/services/persistence/` 改名 `app/services/alarm/` 并按 services 骨架摊平：`PersistenceManager` → `AlarmService`，
单例 `persistence_manager` → `alarm_service`，`strategies/`、`workers/` 子包取消。`app/utils/executor.py` 删除，
它给告警上报提供的重试写进 `alarm_worker.py`。HTTP 契约、Prometheus 指标名与标签值、配置文件名均不变。

## 变更背景

- **现状**：包名 persistence 与职责不符——HLS 落盘早已迁到 recording，本包只剩告警上报（+ TTL 清理，下一批迁出）。
  `GuardedExecutor` 名为通用重试框架，实际唯一调用方是告警 worker，只有一条 `persistence` 策略。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波 C 的第 1 步；
  第 1 波已把 `app.domain` 改名 `app.types`、worker_guard / metrics 下沉 `app.services.utils`。

## 方案详情

### 全景：包内搬迁 + 执行器内联 + 全仓引用改写

```text
app/services/persistence/                  → app/services/alarm/
  manager.py            PersistenceManager →   service.py        AlarmService
  instance.py           persistence_manager →  instance.py       alarm_service
  strategies/alarm_strategy.py             →   reporter.py       AlarmPersistenceStrategy → AlarmReporter
  workers/alarm_worker.py                  →   alarm_worker.py   + 内联重试（_report_with_retry）
  workers/cleanup_worker.py                →   cleanup_worker.py（暂放包根，下一批迁 app/daemons/cleanup/）
  config.py             PersistenceConfig  →   config.py         AlarmServiceConfig / get_alarm_config
  types.py              AlarmPersistenceTask → types.py          AlarmReportTask
app/utils/executor.py                      → 删除
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 包改名与符号改名 | `app/services/alarm/**` | §1 |
| 重试内联 | `app/services/alarm/alarm_worker.py` | §2 |
| 引用改写 | `app/main.py`、`inference/online/temporal/alarm_sink.py`、各处注释、README、DEVELOPMENT §3 | §3 |
| 门禁 | `tests/test_import_hygiene.py` | §4 |

### 1. 包内改名

- `strategies/`、`workers/` 两个子包删除（它们的 `__init__` 只是 re-export）。
- 类名随文件角色改：`AlarmPersistenceStrategy` → `AlarmReporter`（`reporter.py`），`AlarmPersistenceTask` → `AlarmReportTask`，
  `PersistenceConfig` → `AlarmServiceConfig`、`get_persistence_config` → `get_alarm_config`。`AlarmWorker` 的 `strategy` 属性随之改名 `reporter`。
- 公开方法 `persist_alarm()` 不改名（`alarm_sink` 的调用面不动）。
- 日志：`AlarmService` 的启停 / 入队失败日志补 `[AlarmService]` 前缀；配置加载日志 `已加载persistence配置` → `已加载alarm配置`。
  `config/logging.json` 没有按 logger 名配置这些模块，无需改。

### 2. `alarm_worker.py` — `GuardedExecutor` 的 `persistence` 策略内联为 `_report_with_retry`

内联前后逐条对照：

| 项 | 内联前（`GuardedExecutor.execute(policy_name="persistence")`） | 内联后（`_report_with_retry`） |
|----|------|------|
| 最大尝试次数 | `max_attempts=3`（含首次） | `_MAX_ATTEMPTS = 3` |
| 退避 | `min(1.0 × 2^(n-1), 30.0)`，n = 已失败次数 → 1s、2s | `_retry_delay(n)`，同式同参 |
| 重试判据 | `AppError` 且非 `fatal`、`retryable`、n < 3 | `_should_retry`，同 |
| 不重试的 `AppError` | ERROR 日志 + `exc_info`，原样上抛 | 同 |
| 非 `AppError` 异常 | CRITICAL 日志 + `exc_info`，计数，原样上抛，不重试 | 同 |
| 重试日志 | WARNING `[GuardedExecutor] Retry n/3 after Xs (policy=persistence, error=前 100 字)` | WARNING `[AlarmWorker] Retry n/3 after Xs (error=前 100 字)`，改 `%` 占位 |
| `retry_total` | 每次 `AppError` / 未知异常 +1（`operation="persistence"`, `error_type=异常类名`）；重试后成功 +1（`error_type="recovered"`） | 同，`operation` 标签值保持 `"persistence"` |
| `gpu_oom_total` | 异常为 `ModelInferenceError` 且 `is_cuda_error` 时 +1 | 同（告警链路实际触发不到，照搬保持一致） |
| 调用方兜底 | `AlarmWorker._process` 捕获后记 `Report failed after retries` | 不变 |

删掉的只有告警链路用不到的能力：固定延迟策略（`backoff=False`）、`custom_policies`、`on_retry` 回调、未知策略名报错。

测试：`tests/test_exception_handling.py` 改测 `_should_retry` / `_retry_delay` / `_report_with_retry`。
原 12 条中 11 条一一对应改写；`test_calculate_delay_fixed` 测的固定延迟分支已不存在，删除；新增一条未知异常不重试且计数的用例。

### 3. 引用改写

- `app/main.py`：`from .services import alarm, …`，lifespan 嵌套 `alarm.lifespan()` 位置不变。
- `alarm_sink.py`：`from app.services.alarm.instance import alarm_service`；`tests/test_alarm_sink.py` 的 monkeypatch 目标同步。
- `app/utils/__init__.py`：去掉 executor 的 re-export 与 docstring 条目。
- 注释 / docstring 中的旧名（inference、recording、run_control、lab、settings、types、services/utils、stream）、`config/persistence_config.yaml` 的注释、README 目录树与异常处理段、`docs/DEVELOPMENT.md` §3。

### 4. 门禁

`BUDGET` 键 `app.services.persistence` → `app.services.alarm`；`SINGLETONS` 的 `persistence_manager` → `alarm_service`；
`test_persistence_does_not_import_inference` → `test_alarm_does_not_import_inference`。

### 5. 保留项（不改动）

- `config/persistence_config.yaml` 文件名与 `app/settings.py` 字段名（另有配置整理提案负责）。
- `PersistenceError`（`app/types/exceptions.py`）及其 HTTP 响应 `"error": "Persistence failed"`：对外契约。
- Prometheus `retry` 指标的 `operation="persistence"` 标签值。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 告警包 | `persistence/` + `strategies/` + `workers/` 三层 | `alarm/` 一层 |
| 重试实现 | `app/utils/executor.py` 通用框架，唯一调用方是告警 | `alarm_worker.py` 内 50 行 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_exception_handling.py` | 11 passed |
| 全量 `pytest tests/` | 843 passed, 8 skipped |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| TTL 清理仍挂在 `AlarmService` 启停里 | 包职责未收干净 | 下一批迁 `app/daemons/cleanup/` |
| `app/utils/decorators.py` 注释仍提 `GuardedExecutor` | 注释过时 | 该文件由 stream 批次删除 |
