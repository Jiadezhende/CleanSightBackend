# 存储层按 run 定位：`RunIdentity` + `runs.allocate/query`，域读写口与旧签名并存

> **变更状态**：生效中（2026-09-27）——「落盘按运行分目录」第 2 期；新能力已落地，尚无生产调用点
> **知识库**：已沉淀 → [ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)、[DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md)（2026-09-30）

## 概述

新增 [`app/domain/run.py`](../../app/domain/run.py)（`RunIdentity(task_id, step_id, run_id)`）与 [`app/storage/runs.py`](../../app/storage/runs.py)（`allocate` / `query`）。hls、inference 两域的读写口第一个参数改为位置键：传 `RunIdentity` 定位到 `{step}/{run_id}/{domain}/`，旧形态 `(task_id, step_id, ...)` 经装饰器同名并存、行为不变。调用点一个未动。

## 变更背景

- **承接**：[落盘按运行分目录提案](20260927_STORAGE_RUN_DIR_PROPOSAL.md) 第 2 期（§2–§4 的存储层能力），建立在第 1 期 [`_fs` 原语](20260927_STORAGE_FS_PRIMITIVES.md) 之上（写口的「只建一级」用 `_fs.ensure_dir`）。
- **为什么先并存**：第 3 期要把 recording / 离线写入切到 run 目录，第 4 期迁读侧。两期之间新旧调用形态必须同时可用，按 DEVELOPMENT.md §6「新实现先测绿、调用点分批迁」推进。

## 方案详情

### 全景

```text
runs.allocate(task, step)      → RunIdentity，mkdir(parents) {step}/{run_id}/          唯一建产物目录者
runs.query(task, step, run_id) → RunIdentity | None                                   读侧、离线的唯一查询口

域读写口  f(key, ...)，key = _root.RunKey
  RunIdentity(task, step, run_id) → {step}/{run_id}/{domain}/   create 只建域这一级；run 目录不在 → FileNotFoundError
  LegacyStep(task, step)          → {step}/{domain}/            旧布局，create 建到底（迁移期）
  f(task_id, step_id, ...)        ──@legacy_key──→ f(LegacyStep(task_id, step_id), ...)
```

| 部件 | 落在哪 |
|------|--------|
| `RunIdentity` | [`app/domain/run.py`](../../app/domain/run.py) |
| `allocate` / `query` / 可见判据 | [`app/storage/runs.py`](../../app/storage/runs.py) |
| 位置键 `RunKey` / `LegacyStep` / `domain_dir` / `legacy_key` / `run_path` | [`app/storage/_root.py`](../../app/storage/_root.py) |
| 域读写口换成位置键 | `hls/{_layout,_read,_decode,_write}.py`、`inference/{_layout,_detection,_temporal}.py` |

### 1. `runs`

- `allocate(task, step)`：`run_id = max(time_ns() // 1000, 已有最大 run_id + 1)`，再 `mkdir(parents=True)`（不带 `exist_ok`：目录已在说明分配没有串行，直接抛）。调用方须持 `lock_for`。
- `query(task, step, run_id=None)`：
  - 给了 `run_id` → 目录在就返回，不做可见判断；不在 → None。
  - 缺省 → 按 `run_id` 降序返回第一个可见 run；都不可见 → None。
  - 可见 = `hls/metadata.json` 或 `inference/detections.jsonl` 存在。文件名取自两域的 `_layout`，`runs` 不自己拼。
- run 目录名纯数字，旧布局的 `hls/`、`inference/` 被 `dir_name_to_int` 天然跳过；旧布局数据对 `query` 不可见。

### 2. 位置键与迁移期装饰器

- 域内所有路径都经 `_root.domain_dir(key, domain, create=)`。`RunIdentity` + `create=True` 用 `_fs.ensure_dir`（不带 parents）：run 被回收后，写口在建域目录这一步 `FileNotFoundError`，不会重建出僵尸目录。
- 公开读写口与包内路径函数都挂 `@legacy_key`：首参不是位置键时，把前两个位置参数包成 `LegacyStep`。现有调用全部是位置传参，已核对。
- `hls.delete` / `inference.delete` 仍只收 `(task_id, step_id)`：它们只服务首写自清，第 5 期随之删除。
- 包内私有的 `_decode._build_cmd` / `_run_ffmpeg` 首参改为位置键，对应测试随之改传 `RunIdentity`。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| run 身份 | 无 | `RunIdentity`，按值比较、不可变 |
| 盘上 run 目录 | 无 | `runs.allocate` 分配，`runs.query` 查询 |
| 域读写口 | 只收 `(task_id, step_id)` | 另收 `RunIdentity`；旧形态行为不变 |
| 线上行为 | — | 不变（无调用点） |

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 `tests/test_storage_runs.py` | 21 passed：`run_id` 递增（含时钟停滞 / 回拨）、`query` 三种结果与可见判据、旧布局不可见、run 间隔离、四个写口对缺失 run 目录 `FileNotFoundError` 且不建任何目录、回收后迟到写入失败 |
| 全量 `pytest tests/` | 843 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 迁移期同一函数接两种首参 | 类型签名写的是 `RunKey`，传错类型只在运行时 `TypeError` | 第 5 期删 `LegacyStep` / `legacy_key`，读写口只收 `RunIdentity` |
| 新能力没有调用点 | 冲突 #1–#5 仍未闭合 | 第 3 期写侧切换（`start_run` 分配、CQ 改收 `run`、recording / 离线写 run 目录） |
