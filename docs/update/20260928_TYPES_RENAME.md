# 契约包 app/domain 改名 app/types，AppError 体系并入，render 下沉到 inference.online

> **变更状态**：已完成（2026-09-28）——纯搬迁与改名，不改运行逻辑
> **知识库**：已沉淀 → [ARCHITECTURE_PACKAGE_LAYERS.md](../kb/ARCHITECTURE_PACKAGE_LAYERS.md)、[ARCHITECTURE_STORAGE_AND_SCHEMA.md](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md)（2026-09-30）

## 概述

跨层契约包 `app/domain/` 整包改名 `app/types/`；`app/utils/exceptions.py` 并入为 `app/types/exceptions.py`；
只有在线推理用的 `render.py` 下沉到 `app/services/inference/online/`。全仓引用、门禁表、文档里的旧路径同步改名。

## 变更背景

- **现状**：契约分在两处——dataclass 在 `app/domain/`，AppError 体系在 `app/utils/exceptions.py`；`app/domain/render.py` 名为跨层契约，实际只有 `inference.online` 的检测器与可视化在用。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）的第 1 波跨层机械搬迁，本篇是其中第 1 步（契约层）。

## 方案详情

### 全景：三处搬迁 + 引用面改名

```text
app/domain/*            → app/types/*                              （git mv 整包）
app/utils/exceptions.py → app/types/exceptions.py
app/domain/render.py    → app/services/inference/online/render.py
全仓 app.domain → app.types；app.utils.exceptions → app.types.exceptions；app.domain.render → app.services.inference.online.render
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 引用改名 | `app/`、`tests/`、`integration_tests/`、`README.md`、`docs/DEVELOPMENT.md`、`docs/api/ai.md`、`.claude/skills/infer-workflow/` | §1 |
| `app.utils` 门面去掉异常 | `app/utils/__init__.py` 及其异常消费者 | §2 |
| 导入门禁表 | `tests/test_import_hygiene.py` | §3 |

### 1. 引用面

- 绝对导入 `from app.domain.x` / `from app.utils.exceptions` 一律换成 `app.types.x` / `app.types.exceptions`（约 80 个文件，纯文本替换）。
- `render` 的 6 处消费者（`online/detection/{detector,impl/*}.py`、`online/visualization/{visualizer,worker}.py`）都在 `online` 的子包里，目标不是它们的后代，按 §8 写绝对 `app.services.inference.online.render`；`tests/doubles.py` 同。
- `app/main.py` 的 `from .utils import (AppError, ...)` → `from .types.exceptions import (...)`（包内相对不变）。
- 文档：`README.md` 目录树 `domain/` 行改为 `types/`、`utils/` 行去掉「异常」；`app/types/__init__.py` docstring 的文件清单同步（去 fact / render，补 temporal / run / exceptions）。
- `app.types` 与 stdlib `types` 同名不冲突：绝对导入下 `import types` 仍是 stdlib（实测 `import app.types, types` 两者各自正常）。

### 2. `app/utils/__init__.py` 不再 re-export 异常

异常已不在 `app/utils/` 包内，门面去掉 9 个异常名。原经门面取异常的两处改走深路径：

- `app/services/stream/manager.py`：`ConflictError` / `StreamConnectionError` ← `app.types.exceptions`（`log_call` 仍从 `app.utils` 取）。
- `tests/test_boundary_layers.py`：5 个异常 ← `app.types.exceptions`。
- `app/utils/executor.py` 的包内相对 `from .exceptions` → 绝对 `from app.types.exceptions`。

### 3. 导入门禁表

- `BUDGET`：`"app.domain"` 键 → `"app.types"`（预算不变：零重依赖、0.20s；新增的 `exceptions` 是纯 stdlib）。
- `LAYER_PACKAGES`：`app/storage` 与 `app/services/utils` 白名单里的 `app.domain` → `app.types`；相关注释同步改名。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `python -c "import app.types, types"` | 两者均正常，`types` 仍指 stdlib |
| 全量 `pytest tests/` | 838 passed, 8 skipped |
| 残留检查 `git grep app.domain / app/domain / app.utils.exceptions` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/` 与历史 update 记录仍写 `app/domain`、`app/utils/exceptions.py` | 读 KB 时路径过时 | KB 融合时按本篇改名 |
