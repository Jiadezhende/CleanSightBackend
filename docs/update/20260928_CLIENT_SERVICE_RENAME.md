# client 中台改名 ClientService，单例 client_service 移入 instance.py

> **变更状态**：已完成（2026-09-28）——纯搬迁与改名，不改运行逻辑
> **知识库**：待沉淀

## 概述

`app/services/client/manager.py` → `service.py`，`ClientManager` → `ClientService`；全局单例 `client_manager` → `client_service`，
唯一定义处移到新建的 `client/instance.py`；`client/__init__.py` 改为纯 docstring、零 re-export。全仓引用、日志前缀、测试、文档同步。

## 变更背景

- **现状**：client 包的入口叫 `manager.py`，单例定义在类文件尾部，`__init__` 还 re-export 了类与单例，与 services 包骨架（`service.py` 放类、`instance.py` 放单例、`__init__` 零 re-export）不一致。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 2 波 · B 的第 1 步。

## 方案详情

### 全景：一处搬迁 + 一个新文件 + 引用改写

```text
app/services/client/manager.py   → app/services/client/service.py   （ClientManager → ClientService，只放类）
                                   app/services/client/instance.py  （新建：client_service 单例唯一定义处）
app/services/client/__init__.py    纯 docstring，不再导出 ClientManager / client_manager / ClientQueues
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 类与单例拆分 | `app/services/client/{service,instance,__init__}.py` | §1 |
| 引用改写 | `app/**`、`tests/**`、`integration_tests/`、`README.md`、`docs/DEVELOPMENT.md`、`docs/api/` | §2 |

### 1. 类与单例拆分

- `service.py`：logger 由写死的 `"app.services.client.manager"` 改为 `__name__`（= `app.services.client.service`）；`config/logging.json` 未按该名配置，无需改。日志前缀 `[ClientManager]` → `[ClientService]`。
- `instance.py`：`client_service: ClientService = ClientService()`。client 中台是零跨服务依赖的 leaf，不进 `SINGLETONS` 门禁表（沿用原约定）。
- 原来经包根导入的调用方改走深路径：

| 旧 | 新 |
|----|----|
| `from app.services.client import client_manager` / `from app.services.client.manager import client_manager` | `from app.services.client.instance import client_service` |
| `from app.services.client import ClientManager` | `from app.services.client.service import ClientService` |
| `from app.services.client import ClientQueues` | `from app.services.client.queues import ClientQueues` |

### 2. 随名字一起改的标识符

旧名的派生标识符一并改，保证 `git grep client_manager` 零残留：

- 注入形参：`StageAwareDispatcher` / `DetectionService` 的 `client_manager_instance` → `client_service_instance`；`GlobalHealthMonitor(client_manager=...)` → `client_service=...`；对应私有属性 `_client_manager` → `_client_service`。
- 测试：`tests/test_client_manager_find_by_source_ip.py` → `tests/test_client_service_find_by_source_ip.py`；`patch` / `monkeypatch` 目标字符串同步。
- `/api/terminate` 响应 `errors` 数组里注册表子步的前缀 `"client_manager: ..."` → `"client_service: ..."`（`docs/api/api.md` 同步）。字段名 `errors` / `client_cleaned` 不变。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 843 passed, 8 skipped |
| 残留检查 `git grep ClientManager / client_manager / client.manager` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `/api/terminate` 的 `errors` 前缀文本变了 | 若有外部脚本按 `"client_manager:"` 字符串匹配会失配（仓内无此类消费方） | 发现消费方时改其匹配串 |
| `docs/kb/SERVICE_CLIENT_STATE.md` 等仍写 `ClientManager` / `client_manager` | 读 KB 时名字过时 | KB 融合时按本篇改名 |
