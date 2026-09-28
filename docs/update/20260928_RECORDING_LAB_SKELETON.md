# recording / lab 按包骨架改名：_sweeper.py → sweep_worker.py，lab/config.py → runtime_config.py，lab 包根零 re-export

> **变更状态**：已完成（2026-09-28）——纯改名与 import 路径调整，不改运行逻辑；`temp/colorstrip` 的物理搬迁待本地执行（见遗留风险）
> **知识库**：待沉淀

## 概述

`app/services/recording/_sweeper.py` → `sweep_worker.py`；`app/services/lab/config.py` → `runtime_config.py`，
`app/services/lab/__init__.py` 去掉全部 re-export、调用方改走深路径；仓内指向 `app/services/temp/colorstrip/`
的注释改指 `ref/colorstrip/`。对外 HTTP、盘上文件名（`lab_runtime_config.json`）不变。

## 变更背景

- **现状**：services 包骨架规定不用下划线前缀、线程体文件以 `_worker` 结尾；`config.py` 专指「启动时读 yaml 的只读配置」，而 lab 的 `config.py` 是送标页面可改、落 JSON 的运行时状态；包 `__init__` 零 re-export，lab 包根仍在平铺 13 个符号。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波 D 组第 3 步。

## 方案详情

```text
app/services/recording/_sweeper.py   → sweep_worker.py（类 SegmentSweeper 不变）
app/services/lab/config.py           → runtime_config.py
app/services/lab/__init__.py         re-export 清空，只留 docstring
app/services/temp/colorstrip/        → ref/colorstrip/（本地目录，两端均被 .gitignore 忽略，见遗留风险）
```

### 1. recording

- `service.py`：`from ._sweeper import` → `from .sweep_worker import`；持有它的实例属性 `self._sweeper` → `self._sweep_worker`（仅本文件内部使用）；docstring 里的模块名同步。
- `__init__.py` 包结构说明、`instance.py` docstring 同步；`tests/test_recording_service.py` 改 import。
- 日志前缀 `[recording.sweeper]`、线程名 `RecordingSweeper` 保持不变（与类名 `SegmentSweeper` 无关，改它们会动运维可见输出）。

### 2. lab

- `app/routers/lab.py` 只改 import 行：`from app.services.lab import (…)` 拆成 `clip_builder` / `label_studio_client` / `step_exporter` 三条深路径；`from app.services.lab import config as lab_config` → `runtime_config as lab_config`（别名不变，路由体与 `tests/test_lab_tasks_api.py` 对 `lab_router.lab_config` 的 monkeypatch 都不用动）。
- 仓内没有其它经 lab 包根取符号的调用方（测试一直走深路径）。

### 3. temp/colorstrip 的仓内引用

`app/services/algorithm/colorstrip/{__init__.py,grader.py,params.yaml}` 与 `tests/test_algorithm_router.py` 注释里的工装路径改为 `ref/colorstrip/`。`app/` 下没有任何代码 import 该目录。

## 变更效果

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 843 passed, 8 skipped |
| `import app.main` / `app.routers.lab` | 正常 |
| 残留检查 `git grep -E "_sweeper\|lab\.config\|services[./]temp"` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `app/services/temp/colorstrip/` 与 `ref/` 都被 `.gitignore` 忽略，只存在于开发机本地，本分支无法搬迁 | 本地目录仍在旧位置，与注释所指不一致 | 在开发机本地 `mv app/services/temp/colorstrip ref/colorstrip` 并删空的 `app/services/temp/`；随后把 `_bootstrap.py` 的 `parents[4]` 改成 `parents[2]` |
| 工装脚本 `acceptance.py` / `stats.py` / `testset.py` 仍 `from app.algorithm.colorstrip import …`，算法早已迁到 `app.services.algorithm.colorstrip` | 工装在搬迁前就已跑不起来 | 本地搬迁时一并改成 `app.services.algorithm.colorstrip` |
| `docs/kb/` 仍写旧模块名 | 读 KB 时路径过时 | KB 融合时按本篇改名 |
