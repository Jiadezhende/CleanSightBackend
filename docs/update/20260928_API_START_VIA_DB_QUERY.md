# api.start 改走 db_tasks.query_task，router 不再持有 DB session

> **变更状态**：生效中（2026-09-28）
> **知识库**：待沉淀

## 概述

`POST /api/start` 取 `clean_task` 行改为调 `app.db.tasks.query_task`，删掉 router 里的 `get_db` / SQLAlchemy 查询与 `DatabaseError` 包装。
对外响应（404 / 400 / 503 的状态码与 body）逐字不变；唯一行为差异是 DB 连接在查完即还，不再贯穿 `start_run`。

## 变更背景

- **现状**：`app/routers/api.py::start` 自己 `next(get_db())` 开 session、写 ORM 查询、包 `DatabaseError`，并在 `finally` 里关 session——session 从查询一直开到 `asyncio.to_thread(run_control_service.start_run, ...)` 返回，起流期间白占一个连接池连接。
- **承接**：routers 业务逻辑下沉阶段 2；建立在同日 `20260928_DB_QUERY_FUNCS.md` 的 `db_tasks.query_task(task_id) -> Optional[DBTask]`（自开自关 session，SQLAlchemyError → `DatabaseError("Failed to query task {id}", retryable=True, query="task_lookup_by_id")`）之上。

## 方案详情

### 全景

```text
start(req)
  → db_tasks.query_task(req.task_id)       DB 失败 → DatabaseError → 503（db 层抛，文案同旧）
  → None → NotFoundError 404               留 router，原样
  → source_ip 为空 → ValidationError 400   留 router，原样
  → asyncio.to_thread(start_run, ...)      此时已无 DB session
```

| 改动 | 落在哪 |
|------|--------|
| 调用点迁移：`get_db` + `db.query(DBTask)...first()` + try/except/finally → `db_tasks.query_task` | `app/routers/api.py::start` |
| 删 import：`get_db`、`DBTask`、`SQLAlchemyError`、`DatabaseError` | `app/routers/api.py` |
| 测试 patch 点：`patch("app.routers.api.get_db")` + session mock 链 → `patch.object(db_tasks, "query_task")`；新增 404 / 400 / 503 边界用例（503 走真实 `query_task`、让 `SessionLocal` 抛 `SQLAlchemyError`，断言 detail 原文） | `tests/test_api_concurrency.py` |

### 行为差异

| 项 | 旧 | 新 |
|----|----|----|
| DB 连接占用 | 从查询持续到 `start_run` 返回（含起流） | 查完即关 |
| 503 body | `{"error":"Database unavailable","detail":"Database error: Failed to query task {id} [retryable]","retryable":true}` | 同左 |
| 404 / 400 body | — | 同旧 |

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_api_concurrency.py` | 9 passed（原 6 + 新增 start DB 边界 3） |
| 全量 `pytest tests/` | 957 passed, 8 skipped（基线 954 passed, 8 skipped） |

## 遗留风险 / 后续任务

无。
