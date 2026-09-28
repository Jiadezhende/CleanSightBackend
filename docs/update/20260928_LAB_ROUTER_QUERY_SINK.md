# lab router 查询类调用点切到 app/db 与 app/storage 的 query_* 函数

> **变更状态**：生效中（2026-09-28）
> **知识库**：待沉淀

## 概述

[`app/routers/lab.py`](../../app/routers/lab.py) 的任务清单、离线结果判定、段存在判断、label-probs 取时间轴全部改调下层已落地的查询函数；router 不再 import `get_db` / SQLAlchemy / `DBTask` 查询表达式。对外响应除 storage 模式 `updated_time` 可能 +1ms、损坏 npz 不再 500 外不变。送标 / 下载流程未动（下一批）。

## 变更背景

- **现状 / 痛点**：lab router 自己开 DB session 拼 ilike / 分页查询、自己遍历 step 再 `runs.query`、自己逐段算时间跨度、自己解析 temporal / npz 判离线结果——与 task / traceback / ai 里的同类逻辑各写一份。
- **承接**：routers 业务逻辑下沉阶段 1 已新增 `db_tasks.query_task_page`、`hls.query_span` / `query_has_segments`、`runs.query_latest_by_step`、`inference.query_has_offline_results`、`routers/utils/runs.resolve_timeline` 并测绿；本批是 DEVELOPMENT §6 第 3 步「调用点迁移」中 lab 查询类那一批。

## 方案详情

### 全景：`GET /lab-f3m8/tasks` 与 `POST /lab-f3m8/label-probs` 的新调用链

```text
list_lab_tasks
  ├─ task_source == "storage" → _list_storage_tasks → 每个 task:
  │     _list_raw_runs = runs.query_latest_by_step(task_id) 过滤 hls.query_has_segments(run, "raw")
  │     _storage_task_to_item: hls.query_span(run, ("raw",)) 逐 run → min(start_us)//1000, max(end_us)//1000
  └─ 否则 → db_tasks.query_task_page(q, limit=, offset=)   # session 在函数内开关
            → 每行 _task_row_to_item（_list_raw_runs 扫盘，此时 session 已关）
  两种模式的 offline_steps = raw_runs 中 inference.query_has_offline_results(run) 为真的 step

_label_probs_view → resolve_timeline(task_id, step_id, run_id, track)   # 无 run / 该轨无段 → 404
submit_clips 的 404 判定 → hls.query_has_segments(run, "raw")
```

| 调用点（旧） | 新 | 计划编号 |
|------|------|------|
| `list_lab_tasks` 内 `next(get_db())` + ilike / count / order_by / offset / limit + `SQLAlchemyError → DatabaseError` | `db_tasks.query_task_page(q, limit=, offset=)`；strip 在 db 层 | D3 |
| `_storage_task_to_item` 逐段 `list_segments` 算 ts 列表 | `hls.query_span(run, ("raw",))` | S2 |
| `_list_raw_runs` / `submit_clips` 里 `hls.list_segments(run, "raw")` 当布尔用 | `hls.query_has_segments(run, "raw")` | S3 |
| `_list_raw_runs` 遍历 `list_step_ids` + `runs.query` | `runs.query_latest_by_step(task_id)` | S4 |
| `_list_offline_steps` 解析 temporal + `read_label_probs` | `inference.query_has_offline_results(run)` | S5 |
| `_label_probs_view` 手写 `query_timeline` + 空轴 404 | `resolve_timeline(...)`，文案逐字相同 | R1 |

删除的 router import：`sqlalchemy.or_`、`SQLAlchemyError`、`get_db`、`DBTask`（类型标注改 `db_tasks.DBTask`）、`TemporalSegment`、`DatabaseError`。

### 1. 刻意保留

- `_list_raw_runs` / `_list_offline_steps` / `_storage_task_to_item` / `_task_row_to_item` 仍在 router：它们是 DTO 组装（`LabTaskItem`），不是查询。
- `_optional_int` 保留（`_task_row_to_item` 用它转 `current_step` 等列）；needle 转整数改由 db 层 `_as_int` 做。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| storage 模式 `updated_time` | 段尾 = `ts_us + int(EXTINF×1e6)` | `ts_us + round(EXTINF×1e6)`，与 traceback / task 统一，可能比旧值大 1ms |
| `offline_steps` 的 label_probs 部分 | `read_label_probs` 真解析，坏 npz 让整个列表 500 | 只判文件存在，坏 npz 也算「有」、列表不再 500 |
| DB 模式 `has_current_step_raw` 的每行扫盘 | 在 DB session 开着时做 | session 关闭后做（不再占连接） |
| router 行数（wc -l） | 785 | 728 |

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_lab_tasks_api.py` / `test_lab_label_probs.py` / `test_import_hygiene.py` | 78 passed |
| 全量 `pytest tests/` | 953 passed, 8 skipped（基线 954：删掉 1 条与 `test_storage_inference::TestQueryHasOfflineResults` 重复的 `_list_offline_steps` 用例） |

测试改动：`test_lab_tasks_api` 的 DB 替身从 `lab_router.get_db` 改为 monkeypatch `db_tasks.query_task_page`（并断言 q / limit / offset 原样下传）；storage 模式「不碰 DB」的 `_boom` 同样打在 `query_task_page`；`db.closed` 断言删除（session 关闭由 db 层测试覆盖）。`test_storage_task_item_carries_offline_steps` 补上「只有概率」「只有打点」两档与跨 run 的 start / updated_time。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 送标 / 下载 / 探活仍走 router 内旧实现 | 与 `services/lab/service.py` 两份逻辑并存 | 下一批切到 service 并删 router 私有函数 |
| `list_lab_tasks` 是 async 端点里同步查 DB + 扫盘 | 与旧实现相同，阻塞事件循环 | 本批不改行为 |
