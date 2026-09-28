# inference.online 入口改名：manager.py → service.py，InferenceManager → InferenceService

> **变更状态**：已完成（2026-09-28）——纯改名，不改运行逻辑
> **知识库**：待沉淀

## 概述

在线推理入口按 services 包骨架改名：`app/services/inference/online/manager.py` → `service.py`，
类 `InferenceManager` → `InferenceService`，单例 `inference_manager` → `inference_service`（仍在 `instance.py`）。
日志前缀、引用、测试 patch 路径、导入门禁的 `SINGLETONS` 表同步。对外 HTTP / 指标 / 盘上文件不变。

## 变更背景

- **现状**：services 包骨架规定入口文件叫 `service.py`、活体类叫 `<Svc>Service`、单例叫 `<svc>_service`；在线推理入口仍是 `manager.py` / `InferenceManager`。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波 D 组第 1 步。

## 方案详情

```text
app/services/inference/online/manager.py   → service.py
InferenceManager                           → InferenceService
inference_manager（online/instance.py）    → inference_service
```

- 引用面：`app/services/run_control.py`（import 与调用）、`app/services/inference/__init__.py`（`lifespan()` 与导入表 docstring）、`app/services/health_monitor/manager.py`（构造入参 `inference_manager=` → `inference_service=`、属性 `_inference_service`、`_resolve_deps()` 内 import）。
- 仅注释 / docstring 提及的：`routers/health.py`、`client/{config,queues}.py`、`stream/manager.py`、`inference/online/{naming,__init__}.py`、`temporal/actor.py`、`docs/DEVELOPMENT.md` §8 示例、`docs/api/health.md`、`integration_tests/test_single_client.py`。
- 日志前缀 `[InferenceManager]` → `[InferenceService]`；logger 取 `__name__`，`config/logging.json` 无按模块名的配置，不受影响。
- 测试：`patch("app.services.run_control.inference_manager")` → `inference_service`；`tests/test_inference_stage_routing.py` 改 import；健康监控两份测试的构造入参改名；`tests/test_import_hygiene.py` 的 `SINGLETONS` 键改为 `inference_service`。

## 变更效果

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 843 passed, 8 skipped |
| 残留检查 `git grep -E "InferenceManager\|inference_manager\|online\.manager"` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/` 仍写 `InferenceManager` / `manager.py` | 读 KB 时名字过时 | KB 融合时按本篇改名 |
