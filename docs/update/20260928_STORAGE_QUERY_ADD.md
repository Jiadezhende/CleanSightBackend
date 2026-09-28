# storage 新增六个读侧查询（query_*），调用方暂不迁移

> **变更状态**：生效中（2026-09-28）——纯新增，routers 仍走旧的私有实现
> **知识库**：待沉淀

## 概述

`app/storage` 新增 `hls.query_span` + `HlsSpan`、`hls.query_has_segments`、`hls.query_has_init`、
`runs.query_latest_by_step`、`runs.query_lifespan_us`、`inference.query_has_offline_results`，各带单测。
它们收的是 routers 里散落的盘上读取逻辑；本批不动任何调用方，对外 HTTP 行为零变化。

## 变更背景

- **现状**：routers 直接拼 storage 原语算业务量——段时间跨度在 `traceback._step_duration_ms`、`task._summarise_steps`、`lab._storage_task_to_item` 三处各算一遍（口径还不一致：traceback 用 round，lab 用 int）；「遍历 `list_step_ids` 再 `runs.query`」在 task / lab 各写一遍；traceback 用 `run.run_id // 1000` + `runs.successor` 算 run 存续区间，把「run_id = 分配时刻微秒」这条 storage 知识泄漏到了 router。
- **承接**：`app/` 目录结构提案（`docs/update/20260928_APP_LAYOUT_PROPOSAL.md`）「routers 业务逻辑下沉」阶段 1。按 DEVELOPMENT §6 重构四步的第 1、2 步：新实现独立落地并测绿，调用点迁移在阶段 2 分 router 做。

## 方案详情

### 全景：六个查询 → 各自替代的旧实现

| 新函数 | 落在哪 | 替代（阶段 2 迁移） |
|--------|--------|------------------|
| `hls.query_span(run, tracks=TRACKS) -> Optional[HlsSpan]` | `app/storage/hls/_read.py`，形状在 `hls/types.py` | traceback `_step_duration_ms`、task `_summarise_steps` 的跨度部分、lab `_storage_task_to_item` 的起止 |
| `hls.query_has_segments(run, track) -> bool` | `hls/_read.py` | lab `_list_raw_runs`、`submit_clips` 的 `bool(list_segments(run, "raw"))` |
| `hls.query_has_init(run, track) -> bool` | `hls/_read.py` | traceback `_build_vod_playlist` 的 `init_path(...).exists()` |
| `runs.query_latest_by_step(task_id) -> List[RunIdentity]` | `app/storage/runs.py` | task / lab 的「遍历 step 再 `runs.query`」 |
| `runs.query_lifespan_us(run) -> Tuple[int, Optional[int]]` | `runs.py` | traceback `get_task_timeline` 的 `run_id // 1000` + `successor` |
| `inference.query_has_offline_results(run) -> bool` | `app/storage/inference/_temporal.py` | lab `_list_offline_steps` 的逐 run 判定 |

### 1. `HlsSpan` 与 `query_span`：一个函数覆盖三处旧语义

`HlsSpan(tracks, start_us, last_start_us, end_us)`：`tracks` 是有段的轨（保持入参顺序），`start_us` 最早段起点，`last_start_us` 最晚段起点，`end_us` = max(段起点 + round(EXTINF))。全轨无段返回 `None`。三处旧实现的映射：

| 旧实现 | 换成 |
|--------|------|
| traceback `(start_ms, end_ms, duration_ms)`，无段 `(0,0,0)` | `start_us // 1000`、`end_us // 1000`、`max(0, (end_us - start_us) // 1000)`；`None` → `(0,0,0)` |
| task step 摘要 `tracks` / `start_ms` / `last_segment_ms`，两轨都没段丢弃 | `list(span.tracks)`、`start_us // 1000`、`last_start_us // 1000`；`None` → 跳过 |
| lab 存储模式 `start_time` / `updated_time`（多 run、只看 raw） | 各 run `query_span(run, ("raw",))`，取 min(`start_us`) // 1000、max(`end_us`) // 1000 |

测试里每条用例标注钉的是哪一处；另用随机造数（199 个 run、双轨 0–4 段、带随机断流）把三处旧函数与新函数逐字段对拍过，全部一致。

### 2. `query_has_offline_results`：label_probs 只判文件存在

temporal 部分真解析 `temporal.jsonl`，只有 `TemporalEvent` 不算；label_probs 部分只看 `label_probs.npz` 在不在，不再 `np.load`。

### 3. 与 KB `DESIGN_STORAGE_LAYER.md` 的两处冲突（待 kb-merge 裁定）

- **动词前缀「封闭集合」**：KB §1 规定新成员只能用 `list_` / `read_` / `iter_` / `insert_` / … 。本批按目录结构提案「storage 只出 `query_*` / `insert_*`」一律用 `query_*`，现有成员（`list_segments` / `read_temporal` / `successor` …）本批不改名，两套命名暂时并存。
- **hls 读侧「只出两种形状」**：KB §2 卡住 hls 读侧只出段容器与 `Frame`。`HlsSpan` 是第三种（下一批的 `MediaTimeline` / `PlacedSegment` 是第四、五种）。理由：KB 自己的准入判据是「≥2 个消费方」——`HlsSpan` 有 task / traceback / lab 三个，KB 当年撤掉 `Step` 摘要正是因为只有 task 一个消费方。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 段跨度算法 | 3 份，round / int 两种口径 | 1 份（round），迁移后 router 零实现 |
| run 存续区间 | router 知道 run_id = 分配时刻微秒 | 该知识留在 `runs.py` |

**自测结果**

| 项 | 结果 |
|----|------|
| 新增单测（`test_storage_hls` / `_runs` / `_inference`） | 34 条全绿 |
| 全量 `pytest tests/` | 871 passed, 8 skipped（基线 837 passed, 8 skipped；skip 为 worktree 缺模型权重 / ffmpeg 端到端） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 迁移后 lab 存储模式 `updated_time` 由 int 改 round | 个别 EXTINF（如 1.001）下 +1ms，只影响排序 / 展示 | 已接受（路由下沉计划的已定决策） |
| 迁移后 `offline_steps` 对损坏 npz 判「有」 | 原先会 500，现在列出该 step，点开 label_probs 时才报错 | 已接受；列表更快、不因单个坏文件整页 500 |
| 六个函数尚无生产调用方 | 与旧私有实现并存 | 阶段 2 按 router 迁移，最后删旧 |
| KB 命名 / 形状两条规则与本批冲突 | KB 结论过时 | kb-merge 时改写 KB §1 动词表与 §2 形状档数 |
