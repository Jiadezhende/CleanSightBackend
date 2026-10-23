# `stop_run` 的结算、清前端槽、残段落盘拆成独立 try，结算失败不再丢残段

> **变更状态**：生效中（2026-10-23）
> **知识库**：待沉淀

## 概述

`RunControlService.stop_run` 的落盘段原先共用一个 try：`stop_workflow` 或 `persist_alarms` 一抛错，随后的 `flush_residual` 被跳过，
残段与剩余检测结果不落盘就随 `cq.close()` 释放。现拆成三个独立 try，错误分别记为 `settlement:` / `frontend:` / `flush:`。

## 方案详情

| 步骤 | 失败时 | 落在哪 |
|------|--------|--------|
| ① `stop_workflow` + `persist_alarms`（结算告警） | 记 `settlement: …`，继续 | [`app/services/run_control/service.py`](../../app/services/run_control/service.py) |
| ② 清前端槽（`LATEST_TEMPORAL` / `RENDERED`） | 记 `frontend: …`，继续 | 同上 |
| ③ `recording.flush_residual` | 记 `flush: …`；成功才置 `data_flushed` | 同上 |

顺序不变：①→②→③，③ 仍早于注销 CQ。`data_flushed` 现在只反映 ③ 是否成功。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 `test_stop_run_flushes_residual_even_if_settlement_fails`（修复前失败、修复后通过） | passed |
| 全量 `pytest tests/` | 1118 passed |

## 遗留风险 / 后续任务

无。
