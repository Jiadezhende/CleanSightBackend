# 大屏在线/历史任务清单：`GET /task/live` 与 `GET /task/history`

> **变更状态**：生效中（2026-07-29）
> **知识库**：已沉淀 → [ARCHITECTURE_API_SURFACE](../kb/ARCHITECTURE_API_SURFACE.md)（/task/live、/task/history 接线）/ [SERVICE_TRACEBACK_MEDIA](../kb/SERVICE_TRACEBACK_MEDIA.md)（SegmentFinder 枚举能力）(2026-08-02)
>
> 对外契约见 [docs/api/task.md](../api/task.md)；播放端契约见 [ai.md](../api/ai.md)、[traceback.md](../api/traceback.md)。

## 概述

- **改了什么**：`/task` 前缀下新增两个只读清单端点，并把「落盘目录枚举」从 lab router 收敛进 `SegmentFinder`。
- **为什么改**：大屏需要「点清单条目 → 出画面」。排查后发现**播放端早就齐全、参数闭环也已经通**（`WS /ai/video` 双模、`/traceback/task/{id}/playlist.m3u8`），缺的只是清单这一步的正式入口——现有的两个清单都是后台页专用：`/admin-f3m8/clients` 是运维页、`/lab-f3m8/tasks` 是送标页，且后者**只枚举 `raw` 轨**，而 playlist 的 `track` 默认 `processed`，大屏照抄 step 会 404。
- **影响面**：新增 2 个端点；`SegmentFinder` 新增 4 个枚举方法（`list_segments` 行为不变）；`lab.py` 改调共用枚举，**对外响应不变**。

## 大屏能力全景（前端接入看这节）

本次补齐后，大屏的两条链路都**不再需要任何外部输入**——不用业务侧喂 task_id、不用从告警里反推 step_id，清单接口自己就是入口。

```
┌─ 看现在 ────────────────────────────────────────────────┐
│  GET /task/live                                          │
│    → tasks[].task_id / source_ip                         │
│         │                                                │
│         └→ WS /ai/video?task_id=…  或  ?client_id=…      │
└──────────────────────────────────────────────────────────┘

┌─ 看过去 ────────────────────────────────────────────────┐
│  GET /task/history                                       │
│    → tasks[].task_id + steps[].step_id + steps[].tracks  │
│         │                                                │
│         ├→ GET /traceback/task/{task_id}/playlist.m3u8   │
│         │      ?step_id=…&track=…        （喂 hls.js）    │
│         └→ GET /traceback/task/{task_id}/timeline        │
│                ?step_id=…      （进度条告警打点 + 精确时长）│
└──────────────────────────────────────────────────────────┘
```

### 链路一：在线 → 实时画面

`GET /task/live` 出的每条给两个可用参数，**选哪个取决于要绑什么**：

| 用 | 绑定 | 任务结束后 | 该点位起新任务 | 适用 |
|---|---|---|---|---|
| `?task_id=` | 这一次 run | 永久黑屏，不复活 | 不跟随 | 任务详情页、告警联动 |
| `?client_id=<source_ip>` | 这个摄像头点位 | 黑屏等待 | **自动恢复推画面** | 大屏墙、固定点位常亮 |

> 固定点位大屏其实**不必轮询 `/task/live`**：一条 `?client_id=` 的 WS 常连就够，无任务时后端静默（零流量），有任务自动来画面。`/task/live` 的价值在「有几路在跑、分别是哪个点位/阶段」这种全局视图，以及需要按 run 锁定的场景。

### 链路二：历史 → 回放

`GET /task/history` 一次给全回放所需的**三个参数**：`task_id`、`steps[].step_id`、`steps[].tracks`。

前端最小流程：

```js
const { tasks } = await fetch('/task/history').then(r => r.json());

// 每个 task 可能有多个 step，回放是 step 粒度，逐个加载
for (const task of tasks) {
  for (const step of task.steps) {
    const track = step.tracks.includes('processed') ? 'processed' : step.tracks[0];
    const url = `/traceback/task/${task.task_id}/playlist.m3u8`
              + `?step_id=${step.step_id}&track=${track}`;
    // url 直接喂 hls.js，不要自己解析 m3u8
  }
}
```

**这一步取代了原先"从告警里反推 step_id"的绕路**——`GET /task/{task_id}/alarms` 曾是前端拿 step_id 的唯一途径（见 [guide-video.md](../api/guide-video.md)），那要求任务必须产生过告警，无告警的任务根本回放不了。

### 前端必须知道的四条

1. **`track` 从 `steps[].tracks` 里挑，别硬写 `processed`。** playlist 的 `track` 默认就是 `processed`，而只落了 raw 的 step 照默认打过去是 **404**。有了 `tracks` 就不必再「切轨前先 fetch 探一次」。
2. **回放是 step 粒度，不做跨 step 聚合。** 整单回放前端按 `steps[]` 逐个加载。
3. **时间字段只到 step 粒度**：任务级只有 `latest_ms`（排序键 + 「这是什么时候的任务」）。理由见下方专节。
4. **两张清单都只出参数、不出播放 URL**，前端自己拼。播放端的坑（token 300s 过期要重拉 playlist、反代须透传 `X-Forwarded-*`、在途段导致 playlist 短于实际时长）本次没变，仍以 [guide-video.md](../api/guide-video.md) 为准。

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
| **时间字段只到 step 粒度** | 任务级只给 `latest_ms`（排序键 + 展示值），不给 `start_ms`——见下 |
| **`last_segment_ms` 是最后一段的起点** | 不是结束时刻，差一个段长；且为 raw/processed **双轨并集**（与 timeline 同口径，实测两轨边界可差 20+ 秒）。精确时长取 timeline 的 `duration_ms` |
| **不出名称** | `clean_task` 表本就没有名称字段，只有 `task_id` + `source_ip` |
| **`/history` 不返回 `total`** | 固定 10 条，无翻页语义 |

### 为什么任务级不给 `start_ms`

初版给了任务级的 `start_ms` / `last_segment_ms`（= 所有 step 的 min/max），评审时砍掉：

- **回放粒度就是 step**：playlist 必填 `step_id`，跨 step 聚合本期不支持（见 [traceback.md](../api/traceback.md)）。
- **两个 step 之间可以隔任意长时间**：任务级「最早 ~ 最晚」跨过了中间空档，既不是任务时长、也不对应任何可播放的东西。单 step 的任务它纯冗余，多 step 的任务它是个会被读成「任务持续了这么久」的虚数。

保留 `latest_ms`（= `max(steps[].last_segment_ms)`）单值，用途明确到不会误读：清单排序键 + 「这是什么时候的任务」的展示值。**不成对给 start，就不会被当成连续区间。**

## 验证

| 项 | 结果 |
|----|------|
| `tests/test_task_live_history_api.py`（新增 13 例） | 13 passed |
| `tests/test_traceback_segment_finder.py`（补 12 例） | 26 passed |
| lab / traceback 回归 | 47 passed |
| 全量 `pytest tests/` | 368 passed |
| 真实存储目录冒烟 | 本机 `database/` 为空（未跑过落盘任务），返回空清单符合预期；**带数据的端到端验证需在有落盘的机器上补** |
