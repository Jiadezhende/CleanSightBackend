# CQ 身份读者迁到 `cq.run.*`

> **变更状态**：生效中（2026-09-27）——「落盘按运行分目录」第 4 期第三批（4c）
> **知识库**：待沉淀

## 概述

`cq.task_id` / `cq.step_id` 的读者全部改读 `cq.run.task_id` / `cq.run.step_id`：生产代码 10 个文件，测试若干（MagicMock 造的 CQ 改为设 `cq.run`）。这两个转发属性现在没有读者，第 5 期删除。CQ 的告警闸门键去掉 task_id。对外行为不变。

## 变更背景

- **承接**：第 3 期（[写侧切换](20260927_STORAGE_RUN_WRITE_SWITCH.md)）CQ 改收 `run: RunIdentity`，`task_id` / `step_id` 暂留为只读转发属性。按 [提案](20260927_STORAGE_RUN_DIR_PROPOSAL.md) §2 的迁移步骤，第 4 期迁读者，第 5 期删属性。

## 方案详情

| 读者 | 落在哪 |
|------|--------|
| 路由 | `routers/api.py`（terminate）、`routers/task.py`（告警消息装配）、`routers/admin.py`（clients 列表） |
| 编排 | `run_control.py`（幂等判断、重启日志、start 失败的 AppError） |
| 推理 | `inference/online/manager.py`（start / stop_workflow）、`detection/service.py`（写回日志）、`temporal/alarm_sink.py` |
| 流 | `stream/decoder.py`、`stream/manager.py`（异常身份，`cq` / `cq.run` 为空时给 None） |
| CQ 自身 | `client/queues.py`：压力日志身份、启动里程碑日志 |

- **告警闸门键**：原来是 `f"{task_id}:{metric}:{mode}"`。闸门表本就挂在单个 CQ 上，而一个 CQ 就是一个 run，键里的 task_id 恒定，所以去掉，改为 `f"{metric}:{mode}"`，行为等价。这样裸建的 CQ（`run is None`）也不会在这里取属性。
- **测试**：MagicMock 造的 CQ 以前只设 `task_id` / `step_id`，改后要设 `cq.run`；否则代码读 `cq.run.task_id` 拿到的是 Mock，断言碰巧还能过，测的却不是真实值。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 839 passed |
| 临时删掉 CQ 的两个转发属性后跑全量 | 839 passed（确认已无读者，属性随即还原） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 转发属性、读口的 `(task_id, step_id)` 转发（`legacy_reader`）仍在 | 无读者，只是死代码 | 第 5 期单独一次提交删除 |
