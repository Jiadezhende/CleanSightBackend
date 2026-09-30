# 存储 TTL 清理迁出 alarm，落 app/daemons/cleanup/

> **变更状态**：已完成（2026-09-28）——搬迁与改名；cleanup 起停从 AlarmService 拆成独立 lifespan，清理逻辑不变
> **知识库**：已沉淀 → [ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)、[ARCHITECTURE_OVERVIEW.md](../kb/ARCHITECTURE_OVERVIEW.md)（2026-09-30）

## 概述

`app/services/alarm/cleanup_worker.py` → `app/daemons/cleanup/worker.py`（`StorageCleanupWorker` → `CleanupWorker`），
新建 `app/daemons/` 包与 cleanup 的 `__init__`（lifespan）/ `instance`（单例 `cleanup_worker`）/ `config`。
`AlarmService` 不再持有、启停清理 worker；`main.py` 多嵌一层 `cleanup.lifespan()`。配置文件与字段不变。

## 变更背景

- **现状**：TTL 清理只依赖 `app.storage`，与告警无关，却由 `AlarmService.__init__` 按 `enable_cleanup` 构造、随 `start()` / `stop()` 起停，
  配置也和告警共用一个 `AlarmServiceConfig`。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波 C 的第 2 步；建立在同日 persistence → alarm 改名之上。
  提案把「按时钟自驱、不属于任何 run、没有调用方向它下发工作」的后台任务归 `app/daemons/`，routers 只许读其状态。

## 方案详情

### 全景：一个文件迁出 + 配置按段拆开 + lifespan 拆开

```text
app/daemons/__init__.py            新建，纯 docstring
app/daemons/cleanup/
  __init__.py                      新建：lifespan()，enable_cleanup 为假时不起线程
  worker.py                        ← app/services/alarm/cleanup_worker.py；StorageCleanupWorker → CleanupWorker
  instance.py                      新建：单例 cleanup_worker（构造不起线程）
  config.py                        新建：CleanupConfig，读 persistence_config.yaml 的 storage 段

app/services/alarm/config.py       AlarmServiceConfig 只剩 alarm 段
app/services/alarm/service.py      去掉 _cleanup_worker 的构造与起停
app/main.py                        stream > [cleanup, alarm] > recording > inference
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| lifespan 拆分与嵌套位置 | `app/daemons/cleanup/__init__.py`、`app/main.py` | §1 |
| 配置按段拆开 | `app/daemons/cleanup/config.py`、`app/services/alarm/config.py` | §2 |
| 改名与引用 | `worker.py`、storage / lab 注释、tests | §3 |
| 门禁 | `tests/test_import_hygiene.py` | §4 |

### 1. 起停时机

`main.py` 写成 `async with cleanup.lifespan(), alarm.lifespan():`（等价于 cleanup 嵌在 alarm 外层，不改动下面几层的缩进）。

| 时机 | 变更前 | 变更后 |
|------|--------|--------|
| 启动 | `AlarmService.start()`：告警池 → cleanup 线程 | cleanup 线程 → 告警池（同一步里的先后，二者无依赖） |
| 关闭 | 告警池 stop（≤10s，抽干队列）→ cleanup stop（≤5s） | 同 |
| 相对其他服务 | 晚于 health_monitor / stream 起，早于 recording / inference 起；关闭反之 | 同 |
| `enable_cleanup: false` | 不构造 worker | 构造单例但 lifespan 不 `start()` |

### 2. 配置

`config/persistence_config.yaml` 文件名与内容字段不变，两个包各读一段：`storage` 段 → `CleanupConfig`，`alarm` 段 → `AlarmServiceConfig`。
扫描根 `storage_base_dir` 随清理迁到 `CleanupConfig`（仍委托 `settings.storage_base_dir`）。

差异只在配置写坏时：以前任一段有未知字段，整份退回默认值（清理关闭、告警默认参数）；现在只有写坏的那一段退回默认值。

### 3. 改名与引用

- 日志前缀 `[StorageCleanup]` → `[CleanupWorker]`，线程名 `StorageCleanup` → `CleanupWorker`。`config/logging.json` 没有按 logger 名配置，无需改。
- `app/storage/utils/fs.py`、`app/storage/hls/_meta.py`、`app/storage/inference/_detection.py`、`app/services/lab/step_exporter.py` 注释中的清理方指向 `app.daemons.cleanup`；
  `app/settings.py` 注释中读 `storage_base_dir` 的一方从 alarm 改为 cleanup；README 目录树加 `daemons/`。
- `tests/test_storage_cleanup_ttl.py`、`tests/test_storage_tasks.py` 改 import。
- routers 不读清理状态，无改动。

### 4. 门禁

- `BUDGET` 登记 `app.daemons`、`app.daemons.cleanup`（纯 stdlib，0.20s）。
- `SINGLETONS` 登记 `cleanup_worker` → `app.daemons.cleanup.instance`；现唯一引用方是本包 `lifespan()`。
- alarm 包不再 import 任何 cleanup 模块，`app/services/alarm/` 下已无 `cleanup` 引用。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 清理 worker 的属主 | `AlarmService` | `app/daemons/cleanup/` 自己的 lifespan |
| alarm 包职责 | 告警上报 + TTL 清理 | 只剩告警上报 |

**自测结果**

| 项 | 结果 |
|----|------|
| `cleanup.lifespan()` + `alarm.lifespan()` 手动起停（dev 配置 `enable_cleanup: true`） | `CleanupWorker` 线程起、退出后停 |
| 全量 `pytest tests/` | 845 passed, 8 skipped（+2 为新增 BUDGET 条目） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `config/persistence_config.yaml` 文件名已不对应任何包 | 找配置时要看文件头注释 | 由配置整理提案统一改名 |
| `health_monitor` 仍在 `app/services/` | daemons 包只有 cleanup 一个成员 | 按目录提案另批迁移 |
