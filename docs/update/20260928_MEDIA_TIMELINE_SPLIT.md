# 媒体轴拆进 storage.hls，services/utils/media_timeline 只留断流阈值判定

> **变更状态**：生效中（2026-09-28）——搬迁 + 改签名，调用方同批改完，对外 HTTP 行为不变
> **知识库**：待沉淀

## 概述

`MediaTimeline` / `PlacedSegment` 从 `app/services/utils/media_timeline.py` 搬到新建的
`app/storage/hls/_timeline.py`，碰盘的 `MediaTimeline.load` 改成 `hls.query_timeline(run, track)`，新增不带阈值的
`MediaTimeline.wall_gaps()`。`media_timeline.py` 只剩 `GAP_THRESHOLD_MS`、`first_gap(tl)`、`total_gap_ms(tl)`。
调用方（`routers/{ai,traceback,lab}`、`services/lab/clip_builder`）与测试同批机械改名。

## 变更背景

- **现状**：媒体轴（段落点、墙钟↔媒体换算）是纯盘上事实的推导——输入只有清单里的段与 EXTINF，却住在 services 层；`MediaTimeline.load` 在 services 层直接读盘，而 storage 之外本不该有读盘函数。断流判定（0.5s 阈值）则是业务判断，不是盘上事实。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）「media_timeline 拆分：媒体轴 → storage/hls/，断流判定留在本层」；routers 业务逻辑下沉阶段 1。建立在同日 `20260928_STORAGE_QUERY_ADD.md` 的 hls `query_*` 之上。

## 方案详情

### 全景：一刀切在「阈值」上

```text
旧 app/services/utils/media_timeline.py
   PlacedSegment / MediaTimeline（容器、select、换算） → app/storage/hls/_timeline.py（不碰盘）
   MediaTimeline.load(run, track)                      → app/storage/hls/_read.py::query_timeline
   MediaTimeline._gaps（带阈值）                        → MediaTimeline.wall_gaps()（不带阈值）
   GAP_THRESHOLD_MS                                    → 原地保留
   tl.first_gap() / tl.total_gap_ms()                  → first_gap(tl) / total_gap_ms(tl)（基于 wall_gaps 过滤 > 阈值）
```

| 部件 | 落在哪 | 详见 |
|------|--------|------|
| 媒体轴类型 + `wall_gaps` | `app/storage/hls/_timeline.py`，facade 导出 `MediaTimeline` / `PlacedSegment` / `query_timeline` | §1 |
| 断流判定 | `app/services/utils/media_timeline.py` | §2 |
| 调用方改名 | `app/routers/{ai,traceback,lab}.py`、`app/services/lab/clip_builder.py` | §3 |
| 测试与门禁 | `tests/test_storage_hls.py`、`tests/test_media_timeline.py`、`tests/test_lab_clip_builder.py`、`tests/test_import_hygiene.py` | §4 |

### 1. `_timeline.py` 不碰盘，构造归 `_read.py`

`_timeline.py` 只依赖 stdlib + `.types.Segment`；读清单并累加 EXTINF 的那段（原 `load`）挪进 `_read.py::query_timeline`，与 `list_segments` 同模块——hls 域读盘的函数都在 `_read.py`。「EXTINF 落盘精度必须是 ms 整数倍，否则累加 ±1ms 错位」这条约束随之挪到 `query_timeline` 的 docstring。

`wall_gaps()` 逐对产出 `(前段, 后段, 下一段起点 − (本段起点 + EXTINF))`，可正可负，不筛——「多大算断流」不是盘上事实，storage 不持有阈值。

空轨仍用 `hls.MediaTimeline([])` 构造（traceback 无 run、ai / lab 无 run 时）。

### 2. services/utils 只留阈值

`first_gap(tl)` / `total_gap_ms(tl)` 从方法改成模块函数，都是 `tl.wall_gaps()` 过滤 `> GAP_THRESHOLD_MS`，行为与旧方法逐条一致（旧 `_gaps` 就是同一个式子加同一个过滤）。模块只依赖 `app.storage.hls`，仍在 `LAYER_PACKAGES["app/services/utils"]` 白名单内。

### 3. 调用方（纯机械）

| 文件 | 旧 | 新 |
|------|----|----|
| `routers/ai.py` `_temporal_view`、`routers/lab.py` `_label_probs_view` | `MediaTimeline.load(run, track) if run … else MediaTimeline([])` | `hls.query_timeline(run, track) if run … else hls.MediaTimeline([])` |
| `routers/traceback.py` `get_task_timeline` | `MediaTimeline([])` / `MediaTimeline.load` / `timeline.total_gap_ms()` | `hls.MediaTimeline([])` / `hls.query_timeline` / `total_gap_ms(timeline)` |
| `services/lab/clip_builder.py` | `MediaTimeline.load(...).select(...)`、`window.first_gap()`、类型标注 `MediaTimeline` | `hls.query_timeline(...).select(...)`、`first_gap(window)`、`hls.MediaTimeline` |

clip_builder 已 `from app.storage import hls`，故类型标注写 `hls.MediaTimeline`，没有另起 `from app.storage.hls import MediaTimeline`。

### 4. 测试与门禁

- `tests/test_media_timeline.py` 的落点 / 选段 / 双向换算四组用例挪进 `tests/test_storage_hls.py`（改走 `hls.query_timeline`），另加 `wall_gaps` 四条；`test_media_timeline.py` 只留断流判据一组，改成函数调用。
- `BUDGET` 加 `"app.storage.hls._timeline": (set(), 0.40)`；`media_timeline` 那条注释改成「断流判定」。
- `services/utils/__init__.py` 成员表 `media_timeline.py` 的描述同步。

### 5. 与 KB `DESIGN_STORAGE_LAYER.md` 的冲突（待 kb-merge 裁定）

- **hls 读侧「只出两种形状」**（KB §2）：本批再加 `MediaTimeline` / `PlacedSegment`（同日 `HlsSpan` 已是第三种）。理由同 KB 自己的准入判据「≥2 个消费方」：媒体轴有 ai / traceback / lab 三个 router 与 lab 的 clip_builder 四个消费方。
- **动词前缀封闭集合**（KB §1）：`query_timeline` 用 `query_` 前缀，按目录结构提案「storage 只出 `query_*` / `insert_*`」；现有 `list_*` / `read_*` 本批不改，两套命名并存。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| services 层读盘 | `MediaTimeline.load` 在 services/utils 读清单 | 读盘只在 `storage.hls._read` |
| 阈值归属 | 与媒体轴同一个类 | storage 出原始空隙，阈值只在 services/utils |

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 876 passed, 8 skipped（上一批 871；+4 `wall_gaps`、+1 门禁 BUDGET 条目） |
| 残留检查 `git grep` `MediaTimeline.load` / `.first_gap()` / `.total_gap_ms()` / `media_timeline import MediaTimeline` | 除 `docs/kb/`、历史 `docs/update/` 外零残留 |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| `docs/kb/`（`DESIGN_HLS_TIMELINE.md`、`SERVICE_TRACEBACK_MEDIA.md`、`SERVICE_LAB.md`、`ARCHITECTURE_PACKAGE_LAYERS.md`、`TESTING_MAP.md` 等 8 篇）仍写 `MediaTimeline.load` 与 services/utils 位置；`DESIGN_STORAGE_LAYER.md` §2 仍写「只出两种形状」 | 读 KB 时位置过时 | kb-merge 时按本篇改 |
| ai.py / lab.py 的「取时间轴否则 404」仍各写一份 | 重复代码 | 阶段 2 换成 `routers/utils/runs.resolve_timeline` |
