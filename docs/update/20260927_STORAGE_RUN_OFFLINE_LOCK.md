# 离线作业锁定 run：运行中的 run 提交 409，按 run 去重，新增 `reclaimed`

> **变更状态**：生效中（2026-09-27）——「落盘按运行分目录」第 4 期第二批（4b）
> **知识库**：待沉淀

## 概述

离线作业在提交时解析一次 run，整个作业只读写这个 run。该 run 正是当前在跑的 CQ 的 run → 409。作业按 `RunIdentity` 去重，同 step 的不同 run 各跑各的。子进程总带 `--run-id`。点名的 run 不在、或跑到一半被 TTL 回收 → 新状态 `reclaimed`，什么都不写。admin 的提交与查询接口接收可选 `run_id`。

## 变更背景

- **承接**：[落盘按运行分目录提案](20260927_STORAGE_RUN_DIR_PROPOSAL.md) §5；第 3 期 runner 已改为在入口经 `runs.query(task, step)` 锁定最新可见 run（[写侧切换](20260927_STORAGE_RUN_WRITE_SWITCH.md)），4a 读侧已整次锁定 run（[读侧锁定](20260927_STORAGE_RUN_READ_LOCK.md)）。
- **要闭合的问题**：
  - 提案冲突 #2：对正在运行的 step 提交离线作业，会按当时已落盘的部分检测结果出结论。
  - 作业按 (task, step) 去重：同 step 换代后再提交，拿到的是旧 run 的作业，`get` 显示 completed，叠加里却看不到分段。
  - 旧 run 无法指定：同 step 出现更新的 run 后，旧 run 再也跑不了离线。

## 方案详情

### 全景

```text
POST /admin-f3m8/offline/jobs {task_id, step_id, run_id?}
  ├ require_offline(step_id)                      400（先于 run 解析）
  ├ resolve_run(task, step, run_id)               点名不在 / 缺省无可见 run → 404 Run
  └ OfflineJobService.submit(run)
       ├ clients.get(task).run == run → 409        输入还在写
       ├ _jobs[run] 在途 → 返回在途作业             不同 run 各自一个作业
       └ 子进程  cli run --task-id --step-id --run-id
                   runner: runs.query(task, step, run_id)
                     点名的不在 → reclaimed        写时 FileNotFoundError 且 run 已不在 → reclaimed
```

| 部件 | 落在哪 |
|------|--------|
| 提交 / 查询接口 | [`routers/admin.py`](../../app/routers/admin.py)：请求体 / 查询参数加 `run_id` |
| 作业服务 | [`offline/service.py`](../../app/services/inference/offline/service.py)：`submit/get/cancel(run)`、注入 `clients`、`OfflineJob.run_id`、`RECLAIMED` |
| CLI | [`offline/cli.py`](../../app/services/inference/offline/cli.py)：`run` / `query` 加 `--run-id` |
| runner | [`offline/runner.py`](../../app/services/inference/offline/runner.py)：`OfflineRunSpec.run_id`、`reclaimed` |
| 契约 | [`docs/api/admin.md`](../api/admin.md) |

### 1. 409 的判据

`clients.get(run.task_id)` 取当前注册的 CQ，其 `run` 与提交的 run **按值相等**才拒。同 step 的旧 run 照常接收。服务照 `RecordingService` 的写法注入 `clients`，缺省为 `client_manager`（函数体内 import）。

### 2. 去重键从 (task, step) 改为 RunIdentity

提案里比较过另一种做法：「run 不同就取消在途作业」。它不对：显式点旧 run 提交，会把最新 run 的作业取消掉。所以不同 run 各自一个作业，同一 run 在途时返回在途那个。

### 3. `reclaimed`

- runner 入口：点名的 run 目录不在 → `reclaimed`；缺省且没有可见 run → `skipped`（沿用）。
- 写入时 `FileNotFoundError`，且 `runs.query` 确认 run 已不在 → `reclaimed`。`label_probs` 旁路原本吞掉一切写失败，现在对 `FileNotFoundError` 放行，交给这一判断。
- `_RUNNER_STATUSES` 加入 `reclaimed`，否则 CLI 返回的这个状态会被作业服务判成 failed。

### 4. 行为变化

- 缺省 `run_id` 且该 step 没有可见 run：以前 202 入队、子进程报 `skipped`，现在直接 404 Run。
- admin 管理页只发 `{task_id, step_id}`，缺省走最新可见 run，不用改。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| #2 离线 vs 运行中的 run | 按部分输入出结论 | 409 |
| 同 step 换代后再提交 | 返回旧 run 的作业 | 新 run 一个新作业 |
| 旧 run 跑离线 | 做不到 | 带 `run_id` 提交 |
| run 在运行中被回收 | 写失败 → failed | `reclaimed`，不重建目录 |

**自测结果**

| 项 | 结果 |
|----|------|
| 作业服务 | 运行中 run 409 且不留记录；旧 run 在同 step 运行时照收；不同 run 各自成作业、命令带 `--run-id`；`reclaimed` 透传 |
| admin 路由 | 无可见 run / 未知 run_id → 404 Run；运行中 → 409；按 `run_id` 查询；响应带 `run_id` |
| runner / CLI | 点名的 run 不在 → reclaimed 不写；运行中被回收 → reclaimed 且不重建；点名旧 run 只写旧 run；`query` 输出 `run_id` |
| 全量 `pytest tests/` | 839 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 离线在 stop 之后立刻提交 | 拆除时残余的检测结果还在 detections 队列里（毫秒级），离线会缺最后一批 | 按提案接受：离线子进程启动是秒级 |
| admin 管理页不带 `run_id` 轮询 | 轮询期间同 step 起了新 run，缺省解析到新 run，作业查询 404 | 管理页按需改为带上提交响应里的 `run_id`（已写进 admin.md 的静默失败表） |
