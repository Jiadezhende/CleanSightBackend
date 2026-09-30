# 盘上原语收编进 `app/storage/_fs.py`，删除一律经回收区原子完成

> **变更状态**：生效中（2026-09-27）——「落盘按运行分目录」第 1 期
> **知识库**：已沉淀 → [ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)、[DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md)、[DESIGN_STALE_WRITES.md](../kb/DESIGN_STALE_WRITES.md)（2026-09-30）

## 概述

新增 [`app/storage/_fs.py`](../../app/storage/_fs.py)：`replace`（整体替换）、`remove`（rename 进 `{root}/.trash/` 再 rmtree，三态返回）、`purge_trash`、`ensure_dir`（只建一级）。4 份整体替换与 3 份目录删除改为调用它；`cleanup_worker` 每轮先清空 `.trash/`，清空 task 目录那步只认数字目录名。对外行为不变，删除变成原子操作。

## 变更背景

- **承接**：[落盘按运行分目录提案](20260927_STORAGE_RUN_DIR_PROPOSAL.md) 的第 1 期（§1 与 §6 的 `.trash/` 部分）。后续各期的「写者不建父目录」「回收原子」都建立在这一份原语上。
- **现状**：整体替换（tmp + `os.replace`）有 `_idx.write`、`_meta.record_segment`、`_jsonl.write_atomic`、`_temporal._write_probs_atomic` 4 份；删除目录有 `hls.delete`、`inference.delete`、`cleanup_worker` 3 份，都是直接 `rmtree`，失败时留下删了一半的目录（提案冲突 #3、#5）。`_jsonl.write_atomic` 还自带 `mkdir(parents=True)`。提案里列的 `tasks.delete_step` 在当前代码中已不存在。

## 方案详情

### 全景

```text
写  _idx / _meta / _jsonl / _temporal ──→ _fs.replace(path, write_fn)   同目录 .{name}.tmp → os.replace，不建父目录
删  hls.delete / inference.delete    ──→ _fs.remove(dir)               rename → {storage_root}/.trash/{uuid} → rmtree
    cleanup_worker                   ──→ _fs.purge_trash(root=db_dir)  每轮先清回收区
                                         _fs.remove(step, root=db_dir) 删过期 step
```

| 部件 | 落在哪 |
|------|--------|
| 原语 | [`app/storage/_fs.py`](../../app/storage/_fs.py) |
| 整体替换调用方 | `hls/_idx.py`、`hls/_meta.py`、`inference/_jsonl.py`、`inference/_temporal.py` |
| 删除调用方 | `hls/_write.py::delete`、`inference/_layout.py::delete`、[`cleanup_worker.py`](../../app/services/persistence/workers/cleanup_worker.py) |

### 1. `_fs` 的语义

| 原语 | 语义 |
|------|------|
| `replace(path, write_fn)` | `write_fn(tmp)` 写 `.{name}.tmp`，再 `os.replace`；任何异常都先删 tmp 再原样上抛；父目录不在即 `FileNotFoundError` |
| `remove(path, *, root=None) -> Removed` | 不在 → `ABSENT`（且不建 `.trash/`）；rename 失败 → `FAILED`，盘上不动，记 warning；rename 成功 → `REMOVED`，随后 rmtree 失败的残留留在回收区 |
| `purge_trash(*, root=None) -> int` | 清空 `{root}/.trash/`，删不掉的留到下一轮 |
| `ensure_dir(path)` | `mkdir(exist_ok=True)`，不带 parents。本期无调用方，第 2 期写口用 |

- `root` 缺省为存储根（`_root.path()`）。`cleanup_worker` 扫的是注入的 `db_dir`，所以显式传 `root=self.db_dir`，保证回收区与被删目录同卷。
- `_fs` 是包内私有模块，`cleanup_worker` 是唯一的包外调用方（只用 `remove` / `purge_trash`）。
- `hls.delete` / `inference.delete` 保持返回 bool（`remove(...) is REMOVED`），调用方不改；第 5 期随认领表一起删。
- tmp 名统一为 `.{name}.tmp`：`_idx` 从 `raw_segment_X.tmp`、`_meta` 从 `metadata.tmp` 随之改名，二者都不匹配段正则，读侧不受影响。

### 2. `cleanup_worker` 的三处改动

- 每轮 `_scan_and_clean` 先 `purge_trash(root=db_dir)`；
- 删过期 step 改用 `_fs.remove`，只有 `REMOVED` 计数；
- 清空 task 目录只认数字目录名，不再 rmdir 空的 `.trash/`、`.lab_exports/`。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 整体替换实现 | 4 份 | 1 份 |
| 目录删除实现 | 3 份，直接 rmtree | 1 份，rename 进回收区 |
| 删除失败后的盘面 | 删了一半的目录（Windows 上有打开的文件时） | 原样不动（`FAILED`），或已整体移出原位置（`REMOVED`） |
| `_jsonl.write_atomic` | 自带 `mkdir(parents=True)` | 不建父目录（两个调用方本就先 `create=True`） |

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 `tests/test_storage_fs.py` | 三态返回、rename 失败盘上不动、rmtree 失败留回收区、replace 不建父目录 / 失败不留 tmp、purge、ensure_dir 不建 parents |
| `tests/test_storage_cleanup_ttl.py` 新增 | 每轮清回收区、rename 失败 step 保留、空的非数字目录不删 |
| 全量 `pytest tests/` | 821 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 首写自清（`hls.delete` / `inference.delete`）失败时 recording 仍照样记认领 | 冲突 #5 未闭合：删除失败虽不再留半删目录，新旧数据仍会混在同一目录 | 第 3 期写侧切到 run 目录后写侧不再删除，第 5 期删掉这两个函数 |
