# 可视化线程体合并：visualization/pool.py + worker.py → visualization_worker.py

> **变更状态**：已完成（2026-09-28）——纯文件合并，不改运行逻辑
> **知识库**：待沉淀

## 概述

`app/services/inference/online/visualization/` 下的 `worker.py`（`VisualizationWorker`，拉取 + 渲染循环）与
`pool.py`（`VisualizationWorkerPool`，单线程启停）合并为包根的 `visualization_worker.py`；`visualizer.py` 不动。
类名、日志前缀不变，只改 import 路径与注释。

## 变更背景

- **现状**：services 包骨架规定线程 / 进程体文件以 `_worker` 结尾、放包根；`pool.py` 只有一个 80 行的启停壳，与它启停的 worker 拆成两个文件。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波 D 组第 2 步，建立在 `InferenceManager` → `InferenceService` 改名之上。

## 方案详情

```text
visualization/worker.py  ─┐
                          ├→ visualization/visualization_worker.py（VisualizationWorker 在前，VisualizationWorkerPool 在后）
visualization/pool.py    ─┘
visualization/visualizer.py  不变
```

- 合并方式：`git mv worker.py visualization_worker.py`，把 `pool.py` 的 `VisualizationWorkerPool` 类原样追加到文件尾；import 去重后只多出 `guarded_run` 一行。两段模块 docstring 合为一段。
- **类名 `VisualizationWorkerPool` 保留**：它被 `InferenceService.visualization_pool` 持有、日志前缀 `[VisualizationWorkerPool]` 已在运维日志里出现；改名不是骨架要求，本批只做搬迁。
- 引用面：`app/services/inference/online/service.py`（`TYPE_CHECKING` 与 `_build_components()` 内两处 import）、`visualization/__init__.py` 与 `visualizer.py` 的 docstring、`app/services/inference/__init__.py` 包结构说明、`tests/test_viz_throughput_snapshot.py`（import 与 `patch("...visualization.visualization_worker.logger")`）。

## 变更效果

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 843 passed, 8 skipped |
| 残留检查 `git grep -E "visualization\.(pool\|worker)\b"` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/` 仍写 `pool.py` / `worker.py` | 读 KB 时路径过时 | KB 融合时按本篇改名 |
