# storage 私有工具 _fs / _root 收进 app/storage/utils/

> **变更状态**：已完成（2026-09-28）——纯搬迁与改名，不改运行逻辑
> **知识库**：已沉淀 → [DESIGN_STORAGE_LAYER.md](../kb/DESIGN_STORAGE_LAYER.md)（2026-09-30）

## 概述

`app/storage/_fs.py` → `app/storage/utils/fs.py`，`app/storage/_root.py` → `app/storage/utils/root.py`，新建标记型
`app/storage/utils/__init__.py`（纯 docstring、零 re-export）。storage 内部、`cleanup_worker` 与测试的引用、导入门禁表同步。

## 变更背景

- **现状**：storage 层的两份通用能力用下划线前缀文件放在包根，与 `runs.py` / `tasks.py` 等域文件混在一层。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 1 波的第 3 步：每层通用能力落到本层 `utils/`，不用下划线前缀。

## 方案详情

### 全景：两处搬迁 + 引用改写

```text
app/storage/_fs.py    → app/storage/utils/fs.py
app/storage/_root.py  → app/storage/utils/root.py
                        app/storage/utils/__init__.py（新建，标记型）
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 引用改写 | `app/storage/**`、`app/services/persistence/workers/cleanup_worker.py`、`tests/` | §1 |
| 导入门禁表 | `tests/test_import_hygiene.py` | §2 |

### 1. 引用改写：只改 import 行，调用点句柄名 `_fs` / `_root` 不动

按 §8「包内相对、跨包绝对」判：

| 引用方 | 所属包 | 写法 |
|--------|--------|------|
| `utils/fs.py` ↔ `utils/root.py` | `app.storage.utils` | `from . import root as _root` / `from . import fs as _fs` |
| `runs.py`、`tasks.py` | `app.storage`（目标是后代） | `from .utils import root as _root` |
| `hls/*`、`inference/*` | `app.storage.hls` / `.inference`（目标非后代） | `from app.storage.utils import fs as _fs` 等 |
| `cleanup_worker.py`、`tests/*` | 包外 | 绝对 + 同样的别名 |

> 句柄保留 `_fs` / `_root` 别名是为了不动调用点：`root` 这个裸名在 `fs.py`（`remove(..., root=)` 形参）与多份测试里是局部变量，改成 `import root` 会把模块遮掉（`utils/root.py` docstring 那条「别把 helper 命名成 `_root`」同理）。

文档性改名：`app/storage/__init__.py` 的「下划线开头的模块包内私有」清单改为 `utils/root.py` / `utils/fs.py`，
`.trash/` / `DOMAINS` 的出处写成 `utils.fs.remove` / `utils.root.DOMAINS`；`root.py` docstring 的用法示例、
`app/settings.py` 注释、`cleanup_worker` docstring、`tests/test_storage_{fs,tasks}.py` 模块 docstring 同步。

### 2. 导入门禁表

- `BUDGET`：`app.storage._root` / `app.storage._fs` 两行换成 `app.storage.utils.root` / `app.storage.utils.fs`（预算不变：零重依赖、0.20s，实测 ~0.03s）；新增 `app.storage.utils` 包本身一行（标记型，实测 ~0.01s）。
- `LAYER_PACKAGES["app/storage"]` 不变：`utils/` 在 storage 包内，白名单前缀 `app.storage` 已覆盖。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 843 passed, 8 skipped（+1 为新登记的 `app.storage.utils` 包条目） |
| 残留检查 `git grep app.storage._fs / _root、from app.storage import _fs/_root` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/DESIGN_STORAGE_LAYER.md` 等仍写 `_root.py` / `_fs.py` | 读 KB 时路径过时 | KB 融合时按本篇改名 |
