# traceback / ai 两个 router 改走 app.db 与 storage 查询，删掉内联的查库、段跨度、run 区间逻辑

> **变更状态**：生效中（2026-09-28）
> **知识库**：待沉淀

## 概述

`app/routers/traceback.py` 的告警查询、`detected_at` 单位归一、段墙钟跨度、init 存在判断、run 存续区间，
`app/routers/ai.py` 的「取 run + 媒体轴否则 404」，全部改调阶段 1 已落地的下层函数，删掉 router 里被替代的私有函数。
对外响应零变化。traceback 365 → 279 行，ai 264 → 255 行。

## 变更背景

- **现状**：traceback 自己开 session 拼 SQLAlchemy 查询、自己按位数归一 `detected_at`、自己遍历双轨段算墙钟跨度、
  自己用 `run.run_id // 1000` + `runs.successor` 推 run 存续区间（run_id = 分配时刻微秒这条 storage 知识泄漏到 router）；
  ai `_temporal_view` 开头 8 行与 lab `_label_probs_view` 逐字重复。
- **承接**：routers 业务逻辑下沉阶段 2（调用点迁移 + 删旧）；下层函数由同日 `20260928_DB_QUERY_FUNCS.md`、
  `20260928_STORAGE_QUERY_ADD.md`、`20260928_MEDIA_TIMELINE_SPLIT.md`、`20260928_RESOLVE_TIMELINE.md` 落地并测绿。

## 方案详情

### 全景：调用点 → 下层函数

```text
GET /traceback/task/{id}/playlist.m3u8
  resolve_run → hls.list_segments（空 → 404）→ hls.query_has_init（False → 503）→ render_vod

GET /traceback/task/{id}/timeline
  resolve_run
  → hls.query_span(run)             墙钟跨度（双轨并集）；None → (0, 0, 0)
  → hls.query_timeline(run, track)  媒体轴
  → runs.query_lifespan_us(run)     [lo_us, hi_us)，router 自己 // 1000
  → db_alarms.query_step_alarms     DatabaseError → 退化为空 events（不变）
  → db_alarms.detected_at_ms        ≤0 → ValidationError → 400（不变）
  → 按存续区间过滤告警（规则留 router）→ media_ms_at → 按 ts_ms 排序

POST /ai/temporal
  resolve_timeline(task_id, step_id, run_id, track) → read_temporal → 换算媒体刻度
```

| 计划编号 | 旧（router 内） | 新 | 旧函数处理 |
|---------|----------------|----|-----------|
| D5 | `traceback._fetch_task_alarms`（`get_db` + `db.query(DBAlarm)`，返回 dict） | `db_alarms.query_step_alarms(task_id, step_id)`，返回 ORM 行 | 删；`step_id is None` 死分支随之消失 |
| D6 | `traceback._to_ms` | `db_alarms.detected_at_ms` | 删 |
| S2 | `traceback._step_duration_ms` | `hls.query_span(run)` + 三元组换算 | 删 |
| S6 | `hls.init_path(run, track).exists()` | `hls.query_has_init(run, track)` | — |
| S7 | `run.run_id // 1000` + `runs.successor(run)` | `runs.query_lifespan_us(run)` | — |
| R1 | ai `_temporal_view` 内联 resolve_run + query_timeline + Segments 404 | `routers/utils/runs.resolve_timeline` | 内联段删除 |

traceback 不再 import `get_db` / `DBAlarm` / `SQLAlchemyError` / `ValidationError`；ai 不再 import `hls` / `NotFoundError`。

### 行为说明

- 事件字段仍做 `int(alarm_id)`、`step_id` None 透传，与旧 dict 转换逐字段一致。
- ai 的 404 文案 `No {track} segments for task {t} step {s}` 与 `resolve_timeline` 逐字相同；traceback playlist 的 `_no_segments` 文案不同，未合并。
- DB session 在 `query_step_alarms` 内开关，事件组装时已关闭（行只读属性，不受影响）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| traceback.py | 365 行，含 3 个私有业务函数 + SQLAlchemy 查询 | 279 行，只剩协议层（VOD URI 签发、区间过滤、响应组装） |
| ai.py | 264 行 | 255 行 |
| 测试 patch 点 | `tb_router.get_db` + `.query().filter().filter().order_by().all()` mock 链 | `monkeypatch.setattr(db_alarms, "query_step_alarms", ...)` |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_traceback_router.py` | mock 改为替换 `db_alarms.query_step_alarms`，并断言入参 `(task_id, step_id)`；新增 DB 不可用退化、`detected_at` NULL 跳过 / ≤0 → 400 两条 |
| `tests/test_ai_temporal_router.py` | 未改，全绿 |
| 全量 `pytest tests/` | 956 passed, 8 skipped（基线 954 passed, 8 skipped） |

## 遗留风险 / 后续任务

无。
