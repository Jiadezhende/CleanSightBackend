# 大屏在线/历史任务清单：`GET /task/live` 与 `GET /task/history`

> **变更状态**：生效中（2026-07-29）
> **知识库**：待沉淀
>
> 对外契约见 [docs/api/task.md](../api/task.md)；播放端契约见 [ai.md](../api/ai.md)、[traceback.md](../api/traceback.md)。

## 概述

- **改了什么**：`/task` 前缀下新增两个只读清单端点，并把「落盘目录枚举」从 lab router 收敛进 `SegmentFinder`。
- **为什么改**：大屏需要「点清单条目 → 出画面」。排查后发现**播放端早就齐全、参数闭环也已经通**（`WS /ai/video` 双模、`/traceback/task/{id}/playlist.m3u8`），缺的只是清单这一步的正式入口——现有的两个清单都是后台页专用：`/admin-f3m8/clients` 是运维页、`/lab-f3m8/tasks` 是送标页，且后者**只枚举 `raw` 轨**，而 playlist 的 `track` 默认 `processed`，大屏照抄 step 会 404。
- **影响面**：新增 2 个端点；`SegmentFinder` 新增 4 个枚举方法（`list_segments` 行为不变）；`lab.py` 改调共用枚举，**对外响应不变**。

## 关键决策：「已完成」不看 `clean_task.status`

历史清单要「有效且已完成」的任务。判定改用：

> **磁盘上有段（能播） 且 不在 `client_manager` 活跃注册表里（跑完了）**

**不用 `status` 的理由**：该字段后端从不写（[`DBTask`](../../app/models.py) 里 default 是 `"paused"`），取值集合由平台业务侧定义，代码与文档里都查不到。拿它过滤等于把大屏清单挂在一个未知字面量上——写错或平台改值，清单会**静默变空**，且排查时看不出是哪一条件把任务刷没了。而上面两个条件后端都是权威，附带好处是 DB 挂了清单照样出（只是 `source_ip` 为 null）。

副作用（有意为之）：正在跑的任务**不进**历史清单，`POST /api/terminate` 之后才出现。

## 改动详情

### 1. `app/services/traceback/segment_finder.py` — 落盘枚举收敛到一处

新增 `StepRef`（`task_id / step_id / tracks / first_ts_us / last_ts_us`）与四个方法：

| 方法 | 用途 |
|------|------|
| `_scan_step_dir()` | 私有，**单次 `iterdir`** 按轨道分组；双轨枚举只付一次目录遍历 |
| `list_segments()` | 改为 `_scan_step_dir` 的薄封装，**对外行为与返回完全不变**（含 track 非法 `ValueError`） |
| `list_steps(task_id)` | 该 task 下已落盘 step 摘要；两轨都没段的 step 丢弃 |
| `list_task_ids()` | 存储根目录下的数字目录（跳过 `.lab_exports`） |
| `list_task_ids_by_recency()` | **廉价粗排**，排序键 = `max(step 目录 mtime)` |

> `list_task_ids_by_recency` 的边界：段文件写入会更新其所在 **step 目录**的 mtime，故该值 ≈ 最后一段落盘时刻。只 `stat` 目录、不进目录读段文件，成本从 O(总段文件数) 降到 O(目录数)。**mtime 只用于挑深扫候选，绝不对外当时间戳**——响应里的时间一律取 `list_steps()` 的真实 `ts_us`。

### 2. `app/routers/task.py` — 两个端点

`GET /task/live`：迭代 `client_manager.snapshot()`，出 `task_id / source_ip / step_id`。注册表是 COW 不可变 dict，迭代无需加锁。

`GET /task/history`：无查询参数，固定 `_HISTORY_LIMIT = 10`。两阶段，避免每次请求全盘扫段：

1. **粗筛** `list_task_ids_by_recency()` → 剔除活跃 `task_id`
2. **深扫** 按粗筛序逐个 `list_steps()`，无段的丢弃，收满 10 条即停；`_HISTORY_SCAN_CAP = 30` 兜住存储目录病态（大量空 task 目录）时扫穿整个存储
3. 最终按**真实段时间戳**重排（粗筛序基于 mtime，只是近似）
4. `_fetch_source_ips()` 只对最终这 10 个 task 查一次 DB；**整体 `try/except` 吞掉所有 DB 故障**（含建连失败）返回空映射，降级为 `source_ip=null`，不 503——与 `/traceback/task/{id}/timeline` 的降级策略一致

两个端点都写成同步 `def`（非 `async def`），FastAPI 丢进线程池，磁盘扫描与 DB 查询不堵事件循环。

> **竞态**：粗筛与深扫之间任务可能刚起/刚停，清单可能短暂含一个刚起的 run 或漏一个刚停的。大屏下一轮轮询自愈，不加锁。

### 3. `app/routers/lab.py` — 改调共用枚举

- `_list_raw_steps` 从 14 行目录遍历压成一行：`[s.step_id for s in finder.list_steps(task_id) if "raw" in s.tracks]`
- `_list_storage_tasks` 的 task 目录遍历改调 `finder.list_task_ids()`

### 4. 保留项（不改动）

- `_storage_task_to_item` 里的 `list_segments(..., "raw")` **刻意保留**：lab 的 `start_time/updated_time` 是**纯 raw 轨**口径，改用 `StepRef.first/last_ts_us`（双轨并集）会让数值发生位移。存储模式是 DB 停摆时的兜底、低频，多一次扫描可接受，换对外数值零变化。
- lab 要 `total` + 分页，**继续全扫精排**，不用 recency 粗筛。
- `/admin-f3m8/clients`、`/lab-f3m8/tasks` 原样保留——各自服务运维页与送标页，本次不合并。

## 对外约定（前端必读）

| 约定 | 说明 |
|------|------|
| **只出参数，不出 URL** | 清单不返回播放地址，前端自己拼 |
| **`track` 必须从 `steps[].tracks` 挑** | playlist 默认 `processed`，只落 raw 的 step 照默认打就是 404 |
| **`last_segment_ms` 是最后一段的起点** | 不是结束时刻，差一个段长；精确时长取 timeline 的 `duration_ms` |
| **不出名称** | `clean_task` 表本就没有名称字段，只有 `task_id` + `source_ip` |
| **`/history` 不返回 `total`** | 固定 10 条，无翻页语义 |

## 验证

| 项 | 结果 |
|----|------|
| `tests/test_task_live_history_api.py`（新增 13 例） | 13 passed |
| `tests/test_traceback_segment_finder.py`（补 12 例） | 26 passed |
| lab / traceback 回归 | 47 passed |
| 全量 `pytest tests/` | 368 passed |
| 真实存储目录冒烟 | 本机 `database/` 为空（未跑过落盘任务），返回空清单符合预期；**带数据的端到端验证需在有落盘的机器上补** |
