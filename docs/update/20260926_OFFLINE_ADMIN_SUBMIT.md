# admin 离线推理 tab：提交作业 + 作业列表

> **变更状态**：生效中（2026-09-26）
> **知识库**：待沉淀

## 概述

admin 页新增三个端点：`POST /admin-f3m8/offline/jobs`、`GET /admin-f3m8/offline/jobs`、`GET /admin-f3m8/offline/jobs/{task_id}/{step_id}`。
「离线推理」tab 新增一个「运行离线推理」按钮和一张作业列表卡片。正在查看的 step 跑完后，结果会自动刷新。
契约见 [docs/api/admin.md](../api/admin.md#离线推理作业)。

## 变更背景

- **现状**：离线作业服务（[20260926_OFFLINE_JOB_SERVICE](20260926_OFFLINE_JOB_SERVICE.md)）已经上线，但前端没有入口；tab 的提示仍要求到服务器上手敲 CLI。
- **触发来源**：前端需要能手动触发离线推理。

## 方案详情

### 全景

```text
「运行离线推理」(offForm.task_id / step_id)
  → POST /admin-f3m8/offline/jobs → 202 作业对象 | 409 detail 提示（step 在 live / 队满）
离线 tab 可见时 offJobPoller 每 2s → GET /admin-f3m8/offline/jobs → 作业卡片
  某作业从 queued/running 变为结束 → fetchOffTasks() 刷新「有离线结果的任务」
      若状态为 completed 且是当前已加载的 step → fetchOfflineResult() 重画结果
```

| 部件 | 落在哪 |
|------|--------|
| 端点 | [`routers/admin.py`](../../app/routers/admin.py) 末尾「离线推理作业」一节 |
| 按钮、作业卡片、轮询 | [`static/admin/index.html`](../../app/static/admin/index.html) 中的 `runOffline` / `fetchOffJobs` / `offJobPoller` |
| 契约 | [`docs/api/admin.md`](../api/admin.md)；[`docs/api/ai.md`](../api/ai.md) 中 `/ai/temporal` 的「结果由谁产出」同步更新 |

**轮询开关**：由 `syncPollers` 统一管理，和其他 poller 一样，离开该 tab 或页面不可见时停止轮询。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| `tests/test_admin_offline_jobs.py`（5 条：202 → 轮询到 completed、去重、live 返回 409、未知作业返回 404、列表新提交在前） | passed |
| admin 页内联脚本 `node --check` | OK |
| dev 后端实机：POST 返回 202 → 真实子进程约 1.5s 结束 → GET 得到 `skipped`（该 task 无检测结果）；未知键返回 404；`/ui-f3m8/admin/` 返回 200 | 通过 |
| 全量 `pytest tests/` | 962 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 本机没有离线模型权重和存储数据，没有实跑 `completed` 路径和「运行中同键 `/api/start` → superseded」 | 真实模型的耗时与结果，以及换代 kill，只在单测里验证过 | 在有数据和权重的 dev 机上补测；`/api/start` 那项要先与人确认 |
| 前端没有取消按钮 | 排错了只能等作业跑完 | 服务已提供 `cancel()`，需要时再加端点和按钮 |

---

## 追加（2026-09-26）：去掉「step 在 live」的 409 与 `superseded` 状态

随作业服务去掉 live 检查，`POST /admin-f3m8/offline/jobs` 只剩「队满」一种 409，作业状态不再有 `superseded`，
`skipped` 只表示「没有检测结果」。契约已同步 [docs/api/admin.md](../api/admin.md)。
