# run_control 单文件改为包：RunControlService + instance.py 单例

> **变更状态**：已完成（2026-09-28）——纯搬迁与改名，不改运行逻辑
> **知识库**：待沉淀

## 概述

`app/services/run_control.py` 拆成包 `app/services/run_control/`：`service.py` 放类（`RunController` → `RunControlService`），
`instance.py` 放单例（`run_controller` → `run_control_service`），`__init__.py` 纯 docstring。全仓引用、日志前缀、导入门禁同步。

## 变更背景

- **现状**：跨服务编排中枢是 services 下唯一的单文件模块，类与单例同居一处，与 services 包骨架（`service.py` / `instance.py` / 零 re-export 的 `__init__`）不一致。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波 · B 的第 3 步；建立在同波第 1 步 client 改名（`client_service`）之上。

## 方案详情

### 全景：单文件 → 三文件包 + 门禁跟随

```text
app/services/run_control.py  → app/services/run_control/service.py    （RunController → RunControlService，只放类）
                               app/services/run_control/instance.py   （新建：run_control_service 单例唯一定义处）
                               app/services/run_control/__init__.py   （新建：纯 docstring，零 re-export）
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 包内拆分与 import 写法 | `app/services/run_control/` | §1 |
| 导入门禁 | `tests/test_import_hygiene.py` | §2 |
| 引用改写 | `app/routers/api.py`、`app/services/health_monitor/manager.py`、`tests/`、`README.md`、`docs/DEVELOPMENT.md`、`docs/api/api.md`、`config/client_config.yaml` 注释 | §3 |

### 1. 包内拆分

- `service.py` 原来对兄弟服务的 `from .client...` / `from .stream...` 等相对导入全部改成绝对（`app.services.<svc>...`）：文件下沉一层后它们成了跨包依赖（§8）。
- logger 仍是 `getLogger(__name__)`，名字随文件变为 `app.services.run_control.service`；`config/logging.json` 未按该名配置。日志前缀 `[RunController]` → `[RunControlService]`。

### 2. 导入门禁

| 项 | 旧 | 新 |
|----|----|----|
| `SINGLETONS` | `"run_controller": "app.services.run_control"` | `"run_control_service": "app.services.run_control.instance"` |
| `_is_allowed_importer` 编排中枢放行 | `app/services/run_control.py` | `app/services/run_control/service.py` |

`instance.py` 不放行：它只 import 同包的 `RunControlService` 类并构造，不引用任何服务单例；放行只给真正持有跨服务单例的 `service.py`。

### 3. 引用改写

- `from app.services.run_control import run_controller` → `from app.services.run_control.instance import run_control_service`（`routers/api.py`、`health_monitor/manager.py` 函数体内、测试）。
- 测试的 `patch("app.services.run_control.X")` → `patch("app.services.run_control.service.X")`。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 832 passed, 8 skipped（含单例引用面门禁） |
| 残留检查 `git grep RunController / run_controller / run_control.py` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/SERVICE_RUN_CONTROL.md` 等仍写 `RunController` / `run_control.py` | 读 KB 时名字过时 | KB 融合时按本篇改名 |
