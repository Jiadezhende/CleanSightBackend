# task router 改走 db_alarms / db_tasks / hls.query_span / runs.query_latest_by_step

> **变更状态**：生效中（2026-09-28）
> **知识库**：已沉淀 → [ARCHITECTURE_API_SURFACE.md](../kb/ARCHITECTURE_API_SURFACE.md)（2026-09-30）

## 概述

`app/routers/task.py` 的三处数据读取改调下层查询函数：告警历史 → `db_alarms.query_task_alarms`，历史清单补 source_ip → `db_tasks.query_source_ips`，step 摘要 → `runs.query_latest_by_step` + `hls.query_span`。
删掉 `_fetch_source_ips`，router 不再 import `get_db` / SQLAlchemy / ORM 类。对外响应零变化。

## 变更背景

- **现状**：task router 自己开 DB session 写 ORM 查询、包 `DatabaseError`；`_summarise_steps` 自己遍历 `list_step_ids` 再 `runs.query`、再逐轨 `list_segments` 算时间跨度——与 traceback / lab 各算一遍同一件事。
- **承接**：routers 业务逻辑下沉阶段 2；建立在同日 `20260928_DB_QUERY_FUNCS.md`（`query_task_alarms` / `query_source_ips`）与 `20260928_STORAGE_QUERY_ADD.md`（`query_span` / `query_latest_by_step`）之上。

## 方案详情

### 全景

```text
GET /task/{id}/alarms
  db_alarms.query_task_alarms(id)          create_time 降序；失败 DatabaseError → 503（文案同旧）
  → router 组 DTO（原样）

GET /task/history（list_history_tasks 粗排 / 深扫算法不动）
  深扫：_summarise_steps(task_id)
    runs.query_latest_by_step(task_id)     每 step 最新可见 run，step 升序
    → hls.query_span(run)                  双轨并集；None（两轨都没段）→ 丢该 step
    → {step_id, run_id, tracks=list(span.tracks),
       start_ms=start_us//1000, last_segment_ms=last_start_us//1000}
  补点位：db_tasks.query_source_ips(ids)   router 里 try/except Exception → {} + warning（降级不 503，原样）
```

| 调用点 | 旧 | 新 |
|--------|----|----|
| `get_task_alarms` | `get_db` + `db.query(DBAlarm)...` + 包 `DatabaseError` + `finally close` | `db_alarms.query_task_alarms(task_id)` |
| `list_history_tasks` 补 source_ip | 私有 `_fetch_source_ips`（开 session + IN 查询 + 宽泛捕获） | `db_tasks.query_source_ips` + 就地宽泛捕获；**删 `_fetch_source_ips`** |
| `_summarise_steps` | 遍历 `step_tasks.list_step_ids` → `runs.query` → 双轨 `list_segments` 取 min/max | `runs.query_latest_by_step` → `hls.query_span` |

保留在 router 不动：`list_history_tasks` 两阶段算法、`_sort_key`、`_build_signals_10s`、`/live`、`/message`。

### 行为说明

- 告警 503 body `detail` 仍是 `Database error: Failed to fetch alarms for task {id} [retryable]`。
- source_ip 降级仍是**宽泛捕获**（`except Exception`）：`query_source_ips` 只把 SQLAlchemyError 包成 `DatabaseError`，建连期其他异常照旧被 router 吞掉。日志文案不变。
- `_summarise_steps` 的 `start_ms` / `last_segment_ms` / `tracks` 与旧实现逐字段一致（`query_span` 已与三处旧实现对拍）；`query_span` 额外算的 `end_us` 本端点不用。
- DB session 由 db 层自开自关，查完即还。

### 测试

| 文件 | 改动 |
|------|------|
| `tests/test_task_live_history_api.py` | `_install_db` 从替换 `task_router.get_db`（`FakeDB`）改为替换 `db_tasks.query_source_ips`；降级用例 `_boom` 仍抛 `RuntimeError`（验证宽泛捕获） |
| `tests/test_task_alarms_api.py`（新建） | 此前该端点无测试：DTO 映射 + 顺序透传、空结果、DB 失败 503 body 原文 |

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_task_live_history_api.py` + `test_task_alarms_api.py` + `test_task_message_*` | 25 passed |
| 全量 `pytest tests/` | 960 passed, 8 skipped（本批前 957 passed, 8 skipped） |
| `app/routers/task.py` 行数 | 326 → 274 |

## 遗留风险 / 后续任务

无。
