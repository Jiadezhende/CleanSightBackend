# health_monitor 迁入 app/daemons/

> **变更状态**：已完成（2026-09-28）——只搬迁与改名，监控逻辑、配置、HTTP 契约不变
> **知识库**：已沉淀 → [SERVICE_HEALTH_MONITOR.md](../kb/SERVICE_HEALTH_MONITOR.md)（2026-09-30）

## 概述

`app/services/health_monitor/` → `app/daemons/health_monitor/`；`manager.py` → `worker.py`，
`GlobalHealthMonitor` → `HealthMonitorWorker`，单例 `health_monitor` → `health_monitor_worker`（`instance.py`）。
包 `__init__` 改为纯 docstring + `lifespan()`、零 re-export。日志前缀与线程名随类名改为 `HealthMonitorWorker`。

## 变更背景

- **现状**：health_monitor 按时钟自驱、不属于任何 run、没有调用方向它下发工作，却住在 `services/` 下；
  `__init__` 还 re-export 了 `GlobalHealthMonitor` / `HealthMonitorConfig`。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）第 3 波；建立在同日 cleanup 迁入
  `app/daemons/cleanup/` 之上，包骨架与之相同（`__init__` 纯 docstring + lifespan()、`worker.py`、`instance.py`、`config.py` / `types.py`）。
  daemons 可依赖 services，routers 只读其状态。

## 方案详情

### 全景：一个包整体搬迁 + 类 / 单例改名

```text
app/daemons/health_monitor/
  __init__.py     纯 docstring + lifespan()（去掉 re-export）
  worker.py       ← manager.py；GlobalHealthMonitor → HealthMonitorWorker
  instance.py     单例 health_monitor → health_monitor_worker
  config.py / types.py   原样

app/main.py            from .daemons import cleanup, health_monitor（lifespan 嵌套顺序不变，仍在最外层）
app/routers/health.py  改 import 与单例名，读法不变
```

| 部件 | 落在哪 |
|------|--------|
| 搬迁与改名 | `app/daemons/health_monitor/` |
| 引用方 | `app/main.py`、`app/routers/health.py`、`tests/test_reconnect_on_initial_failure.py`、`tests/test_rtsp_read_timeout.py` |
| 注释 / 文档中的路径与类名 | `app/services/client/queues.py`、`app/services/recording/instance.py`、`app/services/stream/config.py`、`app/services/stream/decoder.py`、`config/health_monitor_config.yaml`（仅注释）、`README.md`、`docs/api/health.md`（仅代码路径） |
| 门禁 | `tests/test_import_hygiene.py` |

### 门禁

- `SINGLETONS`：`health_monitor` → `health_monitor_worker`，模块 `app.daemons.health_monitor.instance`。
- `SINGLETON_EXCEPTIONS`：`app/services/health_monitor/manager.py` → `app/daemons/health_monitor/worker.py`；注释补正协作者清单
  （`_resolve_deps()` 里 client / stream / inference / recording 四处，`cleanup_client()` 里另有 run_control_service 一处）。
- `BUDGET`：新增 `app.daemons.health_monitor`、`app.daemons.health_monitor.instance` 两条，重依赖集合为空（实测 ~0.02s / ~0.05s，不拉起任何 `app.services`）。

### 保留项（不改动）

- 配置文件名 `config/health_monitor_config.yaml`、`app/settings.py` 字段名、`config/stream_config.yaml` 的键。
- HTTP 路径与响应字段、Prometheus 指标名。
- `routers/health.py` 的 `get_health_monitor()`（名字不变，返回新单例）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 包位置 | `app/services/health_monitor/` | `app/daemons/health_monitor/` |
| 包公开面 | re-export 类与配置 | 零 re-export，深路径 import |
| 日志前缀 / 线程名 | `[GlobalHealthMonitor]` / `GlobalHealthMonitor` | `[HealthMonitorWorker]` / `HealthMonitorWorker` |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 836 passed, 8 skipped（变更前 834 passed, 8 skipped；+2 为新增 BUDGET 条目） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `test_services_do_not_import_routers` 只扫 `app/services`，health_monitor 迁出后不再受「不许 import routers」约束；`services ↛ daemons` 也无门禁 | 方向违规不会红 | 已补：`test_daemons_do_not_import_routers`、`test_services_do_not_import_daemons` |
| 日志检索关键字从 `[GlobalHealthMonitor]` 变为 `[HealthMonitorWorker]` | 按旧前缀 grep 日志 / 告警规则会漏 | 运维侧知悉 |
