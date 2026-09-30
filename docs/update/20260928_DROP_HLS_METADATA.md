# 删除 `hls/metadata.json`：run 的 hls 可见判据改为「清单里有段」

> **变更状态**：生效中（2026-09-28）
> **知识库**：已沉淀 → [ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)、[DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md)（2026-09-30）

## 概述

`insert_segment` 不再写 `hls/metadata.json`，`_meta.py` 与 `_layout.metadata_path` 删除。`runs` 判断 run 是否可见时，hls 一侧改用 `hls.query_has_segments`（任一轨清单里有段）。对外接口和盘上其余产物不变；盘上已有的 `metadata.json` 不再有读写方。

## 变更背景

- **现状 / 痛点**：`metadata.json` 每写一段做一次整份读改写，维护段数、时长、首末 ts，但**内容没有任何读者**。docstring 里写的两个用途都已失效：TTL 判据早已换成 `{task}/{step}/` 目录的 mtime（`app/daemons/cleanup/worker.py`），清单和大屏的段统计走 `hls.query_span`（读 playlist EXTINF）。它唯一的作用是「文件存在」被 `runs._visible` 当成 hls 可见判据。
- **干扰设计**：一个无人读的派生文件挂着 `start_time`、`created_at` 这类像「开跑时刻」的字段，却只是首段时刻，解析失败时还会按当前段重建。它让人误以为存储层已经有 run 级的时间真源，干扰了 run_id 时间语义收口的讨论。
- **承接**：建立在 run 目录布局 `{task}/{step}/{run_id}/` 与 `hls.query_has_segments` 之上。

## 方案详情

### 全景

```text
旧  insert_segment ③ commit: sidecar → init → 段文件 → 清单条目 → metadata.json
    runs._visible  = hls/metadata.json 存在 or inference/detections.jsonl 存在

新  insert_segment ③ commit: sidecar → init → 段文件 → 清单条目
    runs._visible  = inference/detections.jsonl 存在 or 任一轨 hls.query_has_segments
```

两个判据对真实数据等价：`metadata.json` 只在清单条目追加成功之后才写，存在就说明至少一段已登记；新判据直接问清单，也就是 hls 域「有哪些段」的唯一真源。

| 部件 | 落在哪 |
|------|--------|
| 可见判据 | [`app/storage/runs.py`](../../app/storage/runs.py) `_visible` |
| 写侧去掉统计步 | [`app/storage/hls/_write.py`](../../app/storage/hls/_write.py)；删除 `app/storage/hls/_meta.py` |
| 定位函数 | [`app/storage/hls/_layout.py`](../../app/storage/hls/_layout.py) 删除 `metadata_path` / `_METADATA_NAME` |
| 落盘结构说明 | `app/storage/__init__.py`、`app/storage/hls/__init__.py` 的 docstring |

### 行为差异

- `insert_segment` 少了一个会抛 `OSError` 的步骤。旧实现里统计写失败会让已登记成功的段整体报错。
- `_visible` 由一次 `stat` 变为读清单（最多两条轨）。它只在不带 `run_id` 的查询里、按 run_id 降序逐个试探时调用，通常第一个 run 就命中。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 每段写入的盘操作 | 段 + 清单追加 + metadata 整份读改写（tmp + replace） | 段 + 清单追加 |
| hls 可见判据 | 派生文件存在 | 清单有已登记段（唯一真源） |
| hls 域模块 | 含 `_meta.py` | 删除 |

**自测结果**

| 项 | 结果 |
|----|------|
| 第 1 步（只换可见判据）后全量 `pytest tests/` | 986 passed |
| 第 2 步（删除写侧与定位函数）后全量 `pytest tests/` | 980 passed（删掉 `TestMetadata` 4 个、`test_metadata_counts_the_segment`、`_meta` 导入预算各 1 个） |
| `test_storage_runs` | 可见性用例改为 raw / processed 两轨参数化：只有清单头不可见，有条目即可见 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 盘上已有的 `hls/metadata.json` | 无读写方，只占空间 | 不做迁移；随 step 目录 TTL 回收。一次性毫秒迁移脚本仍会原样搬运它，并把其中的 `start_time` 作为旧布局补建 run 时的 run_id 候选，这一点不受影响 |
| `docs/kb/` 里仍有 `metadata.json` 的描述 | KB 与代码不一致 | 下一次 `/kb-merge` 时沉淀 |
