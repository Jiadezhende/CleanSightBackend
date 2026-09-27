# 删除只剩测试在调用的生产死代码（连同其测试）

> **变更状态**：生效中（2026-09-27）
> **知识库**：待沉淀

## 概述

删除 7 块生产侧零调用方、只被 `tests/` 引用的代码，连同只测它们的用例：persistence 旧 HLS 写侧整套、`traceback/segment_finder` + `offline.Timeline`、utils 的熔断器 / `timing` / `context` / `is_*_error` 及未用重试策略，以及 5 个零散死方法 / 字段 / 别名。生产运行时行为不变。净删约 3200 行，`tests/` 883 → 812 例。

## 变更背景

- **现状 / 痛点**：上一批单测去重（`20260927_TEST_SUITE_DEDUP.md`）审计时逐个 grep 确认：这些符号在 `app/`、`mediamtx_gateway/`、`integration_tests/`、`config/` 里除定义与 re-export 外零调用方，只剩测试在「覆盖」它们，掩盖了无人使用的事实。其中旧 HLS 写侧仍在 `PersistenceManager.__init__` 里被构造（从不启动），自带「重新启用 = 数据静默损坏」的警告。
- **承接**：建立在 HLS 写侧迁入 `app.services.recording`、调用点迁入 `app.storage.hls` 之上。

## 方案详情

### 全景：三块，按生产文件归属分提交

```text
persistence / storage   旧 HLS 写侧 + tasks.delete_step + CQ.get_ca_processed_length
inference / traceback   SegmentFinder + Timeline + StageWorker.infer_batch + CleanSegmenter 别名
  / stream / client     + StreamService.metrics + ClientManager.get_client_count
utils                   CircuitBreaker / RetryExecutorWithCircuitBreaker / timing / context.py
                        / is_retryable_error / is_fatal_error / 未用的 4 个重试策略
```

| 块 | 落在哪 | 详见 |
|----|--------|------|
| persistence / storage | `app/services/persistence/`、`app/storage/tasks.py`、`config/persistence_config.yaml` | §1 |
| inference / traceback / stream / client | `app/services/{traceback,inference,stream,client}/` | §2 |
| utils | `app/utils/` | §3 |

### 1. persistence / storage

- 整删 `strategies/hls_strategy.py`、`workers/hls_worker.py`、`workers/segment_sweeper.py`；`manager.py` 去掉 `hls_queue` / `hls_pool` / `_segment_sweeper` 构造与 `persist_hls_segment` / `flush_residual_segments` / `release_task_locks` / `start_run`；`types.HLSPersistenceTask`、`config.HLSConfig` 与 yaml 的 `hls:` 段删除。`start()` / `stop()` 方法体不变（仍只起告警池与 TTL 清理），`storage_base_dir` 等现役配置保留。
- `storage/tasks.delete_step` 删除（supersede 已改按域懒惰自清，TTL 有自己的删除实现）。
- `ClientQueues.get_ca_processed_length` 删除。
- 测试：整删 `test_hls_eff_fps`、`test_hls_segment_sweeper`、`test_persistence_sink`；删 `TestPurgeStep`、`TestDomainSeam`。其独有覆盖已在上一批迁到现役实现（逆序帧回落、真 CQ 的 `take_*_segment`、persistence 不 import inference 门禁）。

### 2. inference / traceback / stream / client

- 整删 `traceback/segment_finder.py`（读旧平铺布局）；`offline/frame_tracker.py` 删 `Timeline` 及只服务它的 helper，`FrameTracker` 保留（走 `hls.iter_frames`，集成测试在用）。
- `StageWorker.infer_batch` 删除（生产只走子进程 `_infer_models`）；`test_stage_worker_ts_anchor` 改为直接测 `_infer_models` 的 ts 锚定。
- `clean.CleanSegmenter` 兼容别名、`StreamService.metrics`（只写不读）、`ClientManager.get_client_count` 删除。
- `integration_tests/utils.py` 取存储根改读 `settings.storage_base_dir`。
- 测试：整删 `test_traceback_segment_finder`；`test_frame_tracker_boundary` 删 16 条 Timeline 用例。

### 3. utils

- `executor.py`：删 `CircuitBreaker`、`RetryExecutorWithCircuitBreaker`、生产不用的 stream / database / external_api / inference 策略（生产只有 `alarm_worker` 用 `persistence`）、`execute()` 末尾不可达分支。
- 整删 `context.py`（零 setter 调用，`get_client_id()` 恒 None）；`decorators._extract_client_id` 去掉读上下文的兜底（生产 `@log_call` 只装饰 `StreamService` 方法，结果本就恒 None）。
- 删 `decorators.timing`、`exceptions.is_retryable_error` / `is_fatal_error`；`__all__` 补上漏掉的 `ConflictError`。
- 测试：整删 `test_context`；删熔断器 3 条、`timing` 2 条、上下文兜底 1 条；executor 用例统一改用 `persistence` 策略。

### 4. 保留项（不改动）

- `FrameTracker` 与 `hls.iter_frames`：生产暂无入口，为 ROI 提案预留，集成测试在用。
- dispatcher `_admit_to_stage` 接缝、`metadata.json`、`gpu_oom_total`：待 owner 决定。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 生产死代码 | 7 块，约 2400 行 | 0 |
| `tests/` 用例 | 883 | 812 |
| 全量耗时 | 14.2s | 13.5s |

**自测结果**

| 项 | 结果 |
|----|------|
| `import app.main` / `mediamtx_gateway.main` / `integration_tests.utils` | 通过 |
| 全量 `pytest tests/` | 812 passed |
| 复扫被删符号（app / tests / integration_tests / config / README） | 仅余一处历史叙述（`recording/service.py` 论证出处） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/DESIGN_FAULT_TOLERANCE.md` 仍列 5 个策略、CircuitBreaker、`is_*_error`；`TESTING_MAP.md` 仍列 context / Timeline / 旧 HLS 测试 | 仅文档 | KB 融合时更新 |
| `integration_tests/utils.py::seed_hls_segments` 仍把假段写到旧平铺布局 `{step}/raw_segment_*.mp4`，现役读侧只认 `{step}/hls/` | `integration_tests/test_traceback.py` 造的数大概率读不到（预存问题，非本次引入） | 另开任务修 |
| 删 `infer_batch` 后，帧宽高盖章只在主进程 collector 测（`test_infer_proxy`） | 无 | — |
