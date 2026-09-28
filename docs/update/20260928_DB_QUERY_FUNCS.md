# app/db 新增 clean_task / clean_alarm 只读查询函数，并纳入分层门禁

> **变更状态**：生效中（2026-09-28）——新函数已落地并测绿；routers 仍走旧的 `get_db` 内联查询，调用点迁移在下一批
> **知识库**：待沉淀

## 概述

`app/db/tasks.py` 新增 `query_task` / `query_source_ips` / `query_task_page`，`app/db/alarms.py` 新增
`query_task_alarms` / `query_step_alarms` / `detected_at_ms`，逐一对照 `routers/{api,task,lab,traceback}.py`
里的内联查询实现。`app/db` 登记为分层包（白名单 `app.db` / `app.types` / `app.settings`）并逐模块登记导入预算。
本批不改任何 router。

## 变更背景

- **现状**：`clean_task` / `clean_alarm` 的查询内联在 4 个 router 里，各自 `next(get_db())` 取 session、
  各自包 `DatabaseError`；`app/db/` 只有表映射（20260928 DB 包搬家时留下的待办）。
- **触发来源**：routers 业务逻辑下沉，阶段 1 只新增下层能力；迁移调用点在阶段 2 按 router 文件分批做
  （`DEVELOPMENT.md` §6：新实现先独立落地、测绿，再迁调用点，最后删旧）。
- **承接**：建立在 `app/db/` 独立成包（`database.py` / `tasks.py` / `alarms.py`）之上。

## 方案详情

### 全景：router 内联查询 → db 层 query_* 函数

```text
router ──(阶段 2 迁移)──▶ app.db.tasks.query_* / app.db.alarms.query_*
                              │  with SessionLocal() as s:   查完即关
                              │  SQLAlchemyError → DatabaseError（message 与旧 router 逐字相同）
                              ▼
                         返回会话已关闭的 ORM 实例（列属性可读，无 relationship、不 commit）
```

| 新函数 | 取代的旧实现 | 详见 |
|--------|--------------|------|
| `tasks.query_task` | `routers/api.py` `start` 内的按 id 查询 | §1 |
| `tasks.query_source_ips` | `routers/task.py` `_fetch_source_ips` 的查询部分 | §1 |
| `tasks.query_task_page` | `routers/lab.py` `list_lab_tasks` 的 DB 分支 | §1 |
| `alarms.query_task_alarms` / `query_step_alarms` | `routers/task.py` `get_task_alarms`、`routers/traceback.py` `_fetch_task_alarms` | §2 |
| `alarms.detected_at_ms` | `routers/traceback.py` `_to_ms` | §2 |
| 门禁 | `tests/test_import_hygiene.py` | §3 |

### 1. `app/db/tasks.py`

| 函数 | 签名 | 语义（与旧实现一致） | DatabaseError |
|------|------|---------------------|---------------|
| `query_task` | `(task_id: int) -> Optional[DBTask]` | `task_id ==` 取 `.first()` | message `Failed to query task {task_id}`，`query="task_lookup_by_id"` |
| `query_source_ips` | `(task_ids: Sequence[int]) -> Dict[int, Optional[str]]` | 单次 `IN` 查询；DB 里没有的 id 不出现；空输入直接 `{}`、不开 session | message `Failed to query source_ip for tasks`（**新文案**，见下） |
| `query_task_page` | `(needle: Optional[str], *, limit: int, offset: int) -> Tuple[int, List[DBTask]]` | `needle` strip 后为空不筛；非空时 `source_ip` / `status` 各 `ilike('%needle%')`，能 `int()` 时再 OR `task_id ==`；`count()` 后 `updated_time desc, task_id desc` + offset/limit；count 与翻页同一 session | message `Failed to list lab tasks`，`query="SELECT ... FROM clean_task"` |

- `query_source_ips` 的旧实现 `except Exception` 吞掉一切、返回 `{}`，从未构造过 `DatabaseError`，所以没有可沿用的文案。
  新函数只把 `SQLAlchemyError` 包成 `DatabaseError`；**降级（吞错 + warning + 返回 {}）仍由 router 做**，
  且 router 保留宽泛捕获，行为不变。这条文案不会出现在任何响应里。
- `ilike` 的 `%` / `_` 不转义，与旧实现相同。

### 2. `app/db/alarms.py`

| 函数 | 签名 | 语义 | 异常 |
|------|------|------|------|
| `query_task_alarms` | `(task_id: int) -> List[DBAlarm]` | `task_id ==`，`create_time desc` | DatabaseError：message `Failed to fetch alarms for task {task_id}`，`query=f"SELECT ... FROM clean_alarm WHERE task_id = {task_id}"` |
| `query_step_alarms` | `(task_id: int, step_id: int) -> List[DBAlarm]` | `task_id ==` 且 `step_id ==`，`detected_at asc` | DatabaseError：message 同上，`query=None`（旧实现就没传） |
| `detected_at_ms` | `(detected_at: Optional[int]) -> int` | `< 1e11` 秒 ×1000；`< 1e14` 毫秒原样；其余微秒 //1000 | ValidationError：None → `alarm.detected_at is null`；≤0 → `alarm.detected_at must be positive`（field / value 同旧） |

- 两个告警查询共用私有 `_query(filters, order_by, *, task_id, sql=None)`。
- 旧 `_fetch_task_alarms` 的 `step_id is None` 分支（唯一调用方恒传 step_id）是死代码，不搬。
- 所有 `DatabaseError` 都**不传** `task_id=` 等坐标参数：`AppError.__str__` 会把它拼成 `[task=…]`，
  而 503 body 的 `detail` 就是 `str(exc)`。单测按 `str(exc)` 逐字断言。

### 3. 门禁：`app/db` 成为分层包

- `LAYER_PACKAGES["app/db"] = ("app.db", "app.types", "app.settings")`：DB 只读层只认 ORM、连接配置与异常契约，
  不许 import services / storage / routers——否则查询与运行态或盘上产物耦合，DB 与 storage 不再能各自单独降级。
- `BUDGET` 逐模块登记：`app.db` 0.20s（标记型）；`app.db.database` / `tasks` / `alarms` 各 1.0s
  （实测 ~0.35s，主要是 `sqlalchemy.orm` ~0.29s；psycopg2 也在此时加载）。sqlalchemy 不在 HEAVY。
- `app/db/__init__.py` docstring 同步写明边界与新的调用方式。

### 4. session 生命周期变化

旧：router 用 `next(get_db())` 取 session，在 `finally` 里关——`api.start` 的 session 一直开到
`run_control_service.start_run` 返回；`lab.list_lab_tasks` 在 session 开着时逐行扫盘组装 DTO。
新：每个 `query_*` `with SessionLocal() as s:` 查完即关，连接立刻还池。返回的 ORM 实例处于 detached 状态，
只读已加载的列属性，不受影响。迁移后 `api.start` 不再在启动编排期间占一个连接——这是**有意接受**的行为差异。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| clean_task / clean_alarm 查询位置 | 4 个 router 内联 | `app/db` 6 个函数（router 迁移待阶段 2） |
| `app/db` 依赖约束 | 无门禁 | 白名单 + 逐模块导入预算 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_db_queries.py`（内存 SQLite 替换 `SessionLocal`，按真实 SQL 执行；失败路径用未建表引擎触发真 `OperationalError`） | 31 passed |
| `tests/test_import_hygiene.py`（新增 app.db 的 4 条预算、1 条覆盖、1 条白名单） | 68 passed（+6） |
| 全量 `pytest tests/` | 874 passed, 8 skipped（基线 837 passed, 8 skipped；+31 db 查询、+6 门禁） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| routers 仍用旧内联查询，新旧两套并存 | 两处实现可能漂移 | 阶段 2 按 router 文件迁移调用点；旧 `get_db` 用法随之删除 |
| 命名两套并存：db / storage 新函数用 `query_*`，storage 现有 `list_segments` / `read_temporal` 等未改 | 读者需知道两种命名都是读侧 | 另起批次统一，本批不动 |
| 单测用 SQLite，`DESC` 排序里 NULL 的位置与 PostgreSQL 相反 | `updated_time` 为 NULL 的行在两库里排位不同；用例刻意不造 NULL | 生产行为以 PostgreSQL 为准，与旧实现一致 |
