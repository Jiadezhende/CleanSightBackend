# 读侧入口解析一次 run、整次请求锁定；对外增 `run_id` 字段与可选参数

> **变更状态**：生效中（2026-09-27）——「落盘按运行分目录」第 4 期第一批（4a）
> **知识库**：待沉淀

## 概述

回放、时间轴、媒体、ai 时序、lab 四类读口在入口处解析一次 run，之后整个请求只读这个 run。媒体 token 带上 `run_id`，播放途中同 step 换代也不串。列表接口返回 `run_id`，读接口接收可选的 `run_id`；不带时按最新可见 run，老前端不用改。`/timeline` 的告警只保留本 run 存续期内的。`tasks.list_task_ids` 的「最近」排序改为按最大 `run_id`。

## 变更背景

- **承接**：[落盘按运行分目录提案](20260927_STORAGE_RUN_DIR_PROPOSAL.md) §4。第 3 期（[写侧切换](20260927_STORAGE_RUN_WRITE_SWITCH.md)）之后，读口的旧 `(task_id, step_id)` 形态每次调用各自解析最新 run。
- **要闭合的问题**：提案冲突 #4 读侧跨代混读。一个请求里 hls 时间轴与 `temporal.jsonl` 分别解析，换代瞬间新录像会配上旧结果；清单签出的 token 只带 (task, step)，播放中途换代后取到的是新 run 的段。

## 方案详情

### 全景

```text
router 入口  resolve_run(task, step, run_id)          app/routers/_runs.py
               run_id 给了 → runs.query 点名；目录不在 → 404 resource_type="Run"
               缺省        → 最新可见 run；没有 → None，各端点按原「该 step 没数据」响应（404 / 全 0）
     │ 往下只传 RunIdentity
     ├ traceback  playlist：段与 init 的 token 都签 run_id │ timeline：告警按 [run_id, 下一个 run_id) 过滤
     ├ media      token.run_id → runs.query；缺 run_id（旧 token）→ 最新可见 run；run 已回收 → 404
     ├ ai         /ai/temporal：hls 时间轴与 temporal.jsonl 取自同一个 run
     ├ lab        tasks 列表 / label-probs / submit（ClipSpec.run）/ download（StepExporter.export(run)）
     └ task       /live、/history 返回 run_id
```

| 部件 | 落在哪 |
|------|--------|
| 入口解析 | 新增 [`app/routers/_runs.py`](../../app/routers/_runs.py) |
| 路由 | `routers/{traceback,media,ai,lab,task}.py` |
| 服务 | `services/utils/media_timeline.py`（`load(run, track)`）、`services/lab/clip_builder.py`（`ClipSpec(run, …)`）、`services/lab/step_exporter.py`（`export(run, track)`）、`services/traceback/media_token.py` |
| 存储 | `_root.run_ids`、`runs.successor`、`tasks.list_task_ids(order="recent")` |
| 契约 | `docs/api/{traceback,media,task,lab,ai}.md` |

### 1. 缺省 `run_id` 且没有可见 run 时不改成 404

提案写的是「查不到一律 404」，但 `/timeline` 的现有契约是「无段时全 0」，改成 404 会破坏「老前端不用改」。因此只有**点名**的 run 不在时才 404（`resource_type: "Run"`，这是新参数的新行为）；缺省时 `resolve_run` 返回 None，各端点维持原响应：playlist / ai temporal / label-probs / submit / download 仍是 404 Segments，timeline 仍是全 0 且告警不按 run 过滤。

### 2. timeline 告警按 run 存续期过滤

- 区间 `[run.run_id // 1000, successor // 1000)`，单位 ms；最新 run 没有上界。`runs.successor(run)` 返回同 step 下紧接着分配的 run_id，不看可见性。
- 结算告警在 `stop_run` 拆除时生成，而重启是先 stop 再分配新 run，所以结算告警一定落在本 run 的区间内。
- 这是对现状的修正：以前同 step 所有 run 的告警都返回，区间外的被 `media_ms_at` 堆到进度条两端。

### 3. 媒体 token

`MediaTokenPayload` 增加可选 `run_id`（载荷键 `"r"`）。签发清单时解析一次 run，清单里所有段和 init 都用它签。缺 `r` 的 token（上线前签发，最长 `media_token_ttl` = 300 s）按最新可见 run 解析，不因缺字段而校验失败。

### 4. 列表与排序

- `/task/live` 返回 `run_id`（CQ 的 run），`/task/history` 的 `steps[]` 返回 `run_id`（该 step 最新可见 run）。
- lab 任务列表新增 `run_ids: {step_id: run_id}`；`raw_steps` / `offline_steps` 都按各 step 最新可见 run 计算。
- `tasks.list_task_ids` 的排序值由 `"mtime"` 改名为 `"recent"`：取各 step 下最大的 `run_id`（最近一次 run 的开始时刻），`_latest_step_mtime` 删除。唯一调用方 `/task/history` 同步修改。
- **`/task/history` 的排序（2026-09-27 追加修正）**：
  - **问题**：本批最初只把粗排改成最大 `run_id`，终排仍按 `latest_ms`（最后一段时刻），而深扫收满 10 条即停。两个键口径不同，开跑早、结束晚的任务可能在截断时被漏掉。
  - **改为**：终排键 = `max(steps[].run_id)`，即清单里实际列出的 run 中最晚开跑的那个，同值 task_id 大者优先。同一 (task, step) 下没被列出的其他代（更早的、或更新但没段的）不参与。`latest_ms` 只作展示。
  - **截断条件**：粗排键 `tasks.latest_run_id`（数所有 run 目录，不看产物，由原私有函数改为公开）是终排键的上界。因此收满 10 条后不立刻停，下一个候选的上界已低于第 10 名的键才停，结果精确。

## 变更效果

| 冲突 / 维度 | 变更前 | 变更后 |
|------|--------|--------|
| #4 单请求内跨域配对 | ai 叠加的 hls 时间轴与 temporal 各自解析 | 取自同一个 run |
| #4 播放途中换代 | token 只带 (task, step)，后续段取自新 run | token 锁 run；该 run 回收后 404 |
| 跨请求锁定 | 做不到 | 前端带列表返回的 `run_id` 即可 |
| timeline 告警 | 同 step 所有 run 的告警都返回 | 只含本 run 存续期内的 |

**自测结果**

| 项 | 结果 |
|----|------|
| 新增用例 | timeline 按 run 存续期过滤、无 run 时保持全 0；点名未知 run → 404 Run（timeline / playlist）；清单 token 锁 run、回收后 404；缺 `run_id` 的 token 按最新 run 解析；ai temporal 点名旧 run 拿旧结果；lab `run_ids`；`runs.successor`；`list_task_ids(order="recent")` |
| 全量 `pytest tests/` | 828 passed |
| 排序修正 | 按开跑时刻而非段时刻；同 step 更新的空代不参与、不挤掉第 10 名；全量 840 passed |
| dev 端到端（换代期间持续回放） | 通过（2026-09-28 回填，人工执行） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 前端还没带 `run_id` | 回放页的 playlist、timeline、temporal 是独立请求，两次请求之间换代，页面会半新半旧 | 后端能力已就位，前端按需迁移 |
| 读口的旧 `(task_id, step_id)` 转发（`legacy_reader`）仍在 | 路由与 lab 服务已全部改传 RunIdentity；离线 CLI 的 `query` 子命令留到 4b，其余只剩测试与 integration 脚本 | 第 5 期删除 |
| 离线仍按 (task, step) 去重、不挡运行中的 step | 冲突 #2 未闭合 | 4b：离线锁定 run、409、`reclaimed` |
