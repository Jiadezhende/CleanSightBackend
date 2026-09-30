# 删除只剩测试在调用的生产死代码（连同其测试）

> **变更状态**：生效中（2026-09-27）
> **知识库**：已沉淀 → [SERVICE_ALARM.md](../kb/SERVICE_ALARM.md)、[DESIGN_FAULT_TOLERANCE.md](../kb/DESIGN_FAULT_TOLERANCE.md)、[TESTING_MAP.md](../kb/TESTING_MAP.md)（2026-09-30）

## 概述

删除 7 块生产侧零调用方、只被 `tests/` 引用的代码，连同只测它们的用例：persistence 旧 HLS 写侧整套、`traceback/segment_finder` + `offline/frame_tracker.py` 整文件（`Timeline` / `FrameTracker`）、utils 的熔断器 / `timing` / `context` / `is_*_error` 及未用重试策略，以及 5 个零散死方法 / 字段 / 别名。生产运行时行为不变。净删约 3200 行，`tests/` 883 → 802 例。

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

- 整删 `traceback/segment_finder.py`（读旧平铺布局）与 `offline/frame_tracker.py`：`Timeline` 零调用；`FrameTracker.find` 只是 `hls.iter_frames` 外加一层按 ts 逐位配对，同样零调用，不迁入 hls 域——按 ts 取帧的能力就是 `hls.iter_frames`，等 ROI 视觉特征真落地再按需在域内补点查。sidecar 位级保真由 `test_storage_hls` 覆盖。
- `StageWorker.infer_batch` 删除（生产只走子进程 `_infer_models`）；`test_stage_worker_ts_anchor` 改为直接测 `_infer_models` 的 ts 锚定。
- `clean.CleanSegmenter` 兼容别名、`StreamService.metrics`（只写不读）、`ClientManager.get_client_count` 删除。
- `integration_tests/utils.py` 取存储根改读 `settings.storage_base_dir`。
- 测试：整删 `test_traceback_segment_finder`、`test_frame_tracker_boundary`；`integration_tests/test_frame_tracker_roundtrip.py` 去掉 5 项 `find` 检查，改名 `test_hls_frame_roundtrip.py`（剩 9 项全测 `iter_frames` / `read_segment`）。

### 3. utils

- `executor.py`：删 `CircuitBreaker`、`RetryExecutorWithCircuitBreaker`、生产不用的 stream / database / external_api / inference 策略（生产只有 `alarm_worker` 用 `persistence`）、`execute()` 末尾不可达分支。
- 整删 `context.py`（零 setter 调用，`get_client_id()` 恒 None）；`decorators._extract_client_id` 去掉读上下文的兜底（生产 `@log_call` 只装饰 `StreamService` 方法，结果本就恒 None）。
- 删 `decorators.timing`、`exceptions.is_retryable_error` / `is_fatal_error`；`__all__` 补上漏掉的 `ConflictError`。
- 测试：整删 `test_context`；删熔断器 3 条、`timing` 2 条、上下文兜底 1 条；executor 用例统一改用 `persistence` 策略。

### 4. 集成测试造数改用现役布局

`integration_tests/utils.py::seed_hls_segments` 原是手写的旧平铺布局（`{step}/raw_segment_*.mp4`、不登记清单），现役读侧只认 `{step}/hls/` 且「有哪些段」只由清单回答，`test_traceback.py` 造的数读不到。改为复用 `tests/factories.seed_hls_segments` 逐轨铺段（与 `hls.insert_segment` 落盘形态一致），返回值仍是 step 目录，调用方清理逻辑不变；去掉 `base_dir` 参数（唯一调用方不传）。同文件零调用方、读更老布局 `task_{id}/{client}/hls` 的 `check_hls_files` 删除。

### 5. 保留项（不改动）

- `hls.read_segment` / `hls.iter_frames`：生产暂无入口，为 ROI 视觉特征提案预留，单测与集成往返测试在用。
- dispatcher `_admit_to_stage` 接缝、`metadata.json`、`gpu_oom_total`：待 owner 决定。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 生产死代码 | 7 块，约 2400 行 | 0 |
| `tests/` 用例 | 883 | 802 |
| 全量耗时 | 14.2s | 13.5s |

**自测结果**

| 项 | 结果 |
|----|------|
| `import app.main` / `mediamtx_gateway.main` / `integration_tests.utils` | 通过 |
| 全量 `pytest tests/` | 802 passed |
| `integration_tests/test_hls_frame_roundtrip.py`（真 ffmpeg） | 9/9 PASS |
| 集成造数（临时存储根，进程内） | 两轨各 3 段 + init 可被 `hls.list_segments` 读到；`/traceback/task/{id}/playlist.m3u8` 200 且含 3 段；清理后无残留 |
| 复扫被删符号（app / tests / integration_tests / config / README） | 仅余一处历史叙述（`recording/service.py` 论证出处） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/DESIGN_FAULT_TOLERANCE.md` 仍列 5 个策略、CircuitBreaker、`is_*_error`；`TESTING_MAP.md` 仍列 context / Timeline / 旧 HLS 测试 | 仅文档 | KB 融合时更新 |
| 删 `infer_batch` 后，帧宽高盖章只在主进程 collector 测（`test_infer_proxy`） | 无 | — |
