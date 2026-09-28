# routers/utils/runs 新增 resolve_timeline（取 run + 媒体轴，否则 404），调用点暂不替换

> **变更状态**：生效中（2026-09-28）——纯新增，ai / lab 仍各自内联同一段逻辑
> **知识库**：待沉淀

## 概述

`app/routers/utils/runs.py` 新增 `resolve_timeline(task_id, step_id, run_id, track) -> Tuple[RunIdentity, hls.MediaTimeline]`，
收 ai `_temporal_view` 与 lab `_label_probs_view` 里逐字重复的「解析 run → 取该轨媒体轴 → 没有就 404」。
本批不替换这两个调用点；对外行为零变化。

## 变更背景

- **现状**：`app/routers/ai.py::_temporal_view` 与 `app/routers/lab.py::_label_probs_view` 开头 8 行完全相同：`resolve_run` → `hls.query_timeline`（无 run 时空轨）→ 空则 `NotFoundError("No {track} segments for task {t} step {s}", resource_type="Segments", resource_id="task=…,step=…,track=…")`。
- **承接**：routers 业务逻辑下沉阶段 1；建立在同日 `20260928_MEDIA_TIMELINE_SPLIT.md` 的 `hls.query_timeline` / `hls.MediaTimeline` 之上。

## 方案详情

### 全景

```text
resolve_timeline(task_id, step_id, run_id, track)
  → resolve_run(...)                  点名 run 不在 → Run 404（原样）
  → hls.query_timeline(run, track)    无可见 run → 空轨
  → 空轨 → Segments 404（文案、resource_type、resource_id 与两处现有实现逐字相同）
  → (run, timeline)                   run 此时必非 None
```

| 部件 | 落在哪 |
|------|--------|
| `resolve_timeline` | `app/routers/utils/runs.py` |
| 单测 | `tests/test_router_utils_runs.py`（新建） |
| 包成员描述 | `app/routers/utils/__init__.py` docstring |

返回 `run` 非 Optional：非空轨只可能来自非 None 的 run，调用方不必再判。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_router_utils_runs.py` | 5 passed（最新 run、点名 run、无 run 404、该轨无段 404、点名不中 Run 404） |
| 全量 `pytest tests/` | 881 passed, 8 skipped |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| ai / lab 两处仍内联旧逻辑 | 与新函数重复 | 阶段 2 替换为 `run, timeline = resolve_timeline(...)` 并删内联代码 |
