> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Persistence Service

持久化服务现在**只剩两件事**：告警上报（异步 HTTP）与存储 TTL 回收。它是**无状态落库层**——过闸/去重/归属编排都不在此。

**HLS 落盘已整个不在本服务**：写侧是 `app/services/recording/`（见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)），格式归 `app/storage/hls`（见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md) 与 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)）。

## PersistenceManager 启动的两件

`PersistenceManager.start()` 只起：

- `alarm_pool`（`AlarmWorkerPool`，1 worker）：消费 `alarm_queue`，异步 HTTP 上报。
- `_cleanup_worker`（`StorageCleanupWorker` daemon 线程）：存储 TTL 回收，由 `storage.enable_cleanup` 开关（生产 yaml 置 `true`）。

`stop(timeout)` 与之对称：只停这两个，停 alarm_pool 时会 drain 队列尽量不丢。

对外方法面里日常只用一个：`persist_alarm(alarm_info: Dict) -> bool`，**纯入队**，无过闸/去重。

> `persist_hls_segment` / `flush_residual_segments` / `start_run(cq)` / `release_task_locks` 仍留在类上但**生产已无调用点**（`run_control` 的残段 flush 与代次回收都改调 recording，start 侧的 purge 整个删掉）；只剩 `tests/test_persistence_sink.py` 还在打桩调 `flush_residual_segments`。

## ⚠ 护栏：旧 HLS 四件套仍在 `__init__` 里构造，但绝不能启动

`hls_queue` / `hls_pool`（`HLSWorkerPool`）/ `_segment_sweeper`（`HLSSegmentSweeper`）仍在 `__init__` 里被构造——旧实现尚未删除，直接 `PersistenceManager()` 打桩的测试还依赖它们存在——但 `start()` **刻意不启动它们**。

**把那两行加回去 = 数据静默损坏**：`HLSSegmentSweeper` 与 recording 的 `SegmentSweeper` 都从活跃 CQ **破坏性 drain**。两个同时跑的结果是各拿走一半帧，产出两份互相缺帧、时间轴却都自洽的段，**两端都不报错**。要恢复旧路径，必须先停掉 recording。

`config/persistence_config.yaml` 里的 `hls:` 整节（`workers` / `queue_size` / `sweep_interval_seconds`）随之成为死配置，只被那几个不启动的对象读。

## 告警落库归属（无状态）

`persist_alarm(alarm_info)` 只做入队。**过闸去重（5s 冷却）+ mode 归属 + 别名烧录在 inference 侧**：`ClientQueues.append_alarm_record_with_gate` 管去重，`inference/temporal/alarm_sink.persist_alarms` 管编排（实时 / 结算），持久化只读 `alarm_info` 里已定好的字段落库。

`AlarmWorker` 用 `GuardedExecutor`（3 次、指数退避）调 `AlarmPersistenceStrategy.report_alarm()` HTTP POST 到 `settings.alarm_report_url`。

> 边界固定：告警过闸/编排归属 inference 域，**不迁入 persistence**。

配置：`alarm.workers: 1`、`alarm.queue_size: 200`。

## 存储 TTL 回收

`StorageCleanupWorker` 周期（`cleanup_interval_seconds`，默认 3600s）扫 `{db_dir}/{task_id}/{step_id}/`，**两级都只认十进制数字目录名**，删除目录自身 mtime 超 `cleanup_days`（生产 15 天）的 step 目录，顺手 `rmdir` 被掏空的 task 父目录。

**判据 = `{task}/{step}/` 目录自身的 `st_mtime`，不下钻域子目录。** 旧判据（`glob("*/*/metadata.json")` 读 `updated_at`）已作废：产物按域隔离后 `metadata.json` 落进了 `{step}/hls/`，那个 glob 匹配不到它，表现是**新数据永不回收、老数据照常回收**——单向漏盘且无任何日志。目录 mtime 对平铺与分域两种布局一视同仁，同时消解「只有 features.jsonl、没有 HLS 段的 step 永不回收」那类泄漏。

目录 mtime 只在**增删直接子项**时变，而 `{step}/` 的直接子项都是 run 起始那几秒建好的——`hls/` 域目录，以及尚未迁入数据层的 `features.jsonl` / `facts.jsonl`（见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)）。此后每段落盘动的是 `hls/` 的 mtime，`features.jsonl` 的 append 也不动父目录，`{step}/` 纹丝不动，故它是创建时间的好代理。（Linux + Python 3.11 拿不到真正的创建时间。）

### 两条已知偏差（设计后果，不是漂移）

1. **`lab/` 是延迟创建的**：某 step 第一次被导出送标时才建这个子目录，那一刻 `{step}/` 的 mtime 被刷新、TTL 计时重置。正在被反复导出的 step 因此天然不被回收——白捡的续命，不是 bug。
2. **活跃 step 不再免疫**：旧判据下 `updated_at` 每 ~10s 刷新一次，跑着的 step 永远删不掉；换判据后，一个连续跑满 `cleanup_days` 的 step 会被删掉自己正在写的录像。当前任务超时 30 分钟、触发不到，但**把 `cleanup_days` 调小或引入长跑任务时会真的发生**。

### ⚠ 不要改成复用 `app.storage.tasks.list_task_ids(order="mtime")`

那个口径**下钻域子目录取最大值**，答的是「最近活动」不是「创建」——每写一段就续一次命，等于永不回收。两个口径分开是刻意的。同理，本 worker 扫的是注入的 `self.db_dir` 而不复用 `tasks.list_step_ids`（后者一律从 settings 解析路径）：让删除动作认一个它自己没扫过的根是不必要的错位风险。

数字目录名过滤也不是洁癖：存储根下还住着 `.lab_exports/`（lab 导出临时件，自带 30 分钟孤儿扫描），旧判据靠「有没有 `metadata.json`」把它天然挡在外面，换判据后必须显式挡。

## 存储根单一真源

存储根统一读 `settings.storage_base_dir`（`PersistenceConfig.storage_base_dir` 转发）。persistence 不反向摸 inference，inference 也不摸 persistence 私有 `db_dir`——各方经 settings 对齐同一根。

## 代码来源

- `app/services/persistence/manager.py`（`start()` 只起两件 + 不启动护栏）
- `app/services/persistence/workers/{alarm_worker,cleanup_worker}.py`
- `app/services/persistence/strategies/alarm_strategy.py`
- `app/services/persistence/{config,types,instance}.py`
- `app/services/inference/temporal/alarm_sink.py`（过闸编排归属）
- `app/settings.py`（`storage_base_dir` / `alarm_report_url`）
- `config/persistence_config.yaml`
- `tests/test_storage_cleanup_ttl.py`、`tests/test_alarm_sink.py`
