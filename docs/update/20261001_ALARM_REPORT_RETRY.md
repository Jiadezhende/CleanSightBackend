# 告警 HTTP 上报失败恢复重试；告警链路去掉 client_id

> **变更状态**：生效中（2026-10-01）
> **知识库**：待沉淀

## 概述

告警 HTTP 上报失败时恢复按退避重试（最多 3 次）。告警上报链路（`alarm_sink` → `AlarmReportTask` → `AlarmReporter`）不再携带 `client_id`，标识统一为 `task_id` + `step_id`；外部上报 payload 不变。

## 变更背景

- **现状**：[`AlarmReporter.report_alarm`](../../app/services/alarm/reporter.py) 上报失败时抛 `PersistenceError(client_id=...)`，而 `PersistenceError.__init__` 不接受 `client_id`，实际抛出 `TypeError`。`_report_with_retry` 把它当非 `AppError` 处理：记 CRITICAL、不重试，这条告警直接丢失。原有测试只用手工构造的 `PersistenceError` 测重试策略，没走真实 reporter，覆盖不到。
- **`client_id` 已无用**：它在告警链路里的值是 `cq.source_ip`，只用于传递和一条日志，外部 payload 从未包含它；run 标识早已是 `task_id` + `step_id`。
- **触发来源**：2026-09-30 KB 审校时核出（[SERVICE_ALARM.md](../kb/SERVICE_ALARM.md) 已按旧行为记为缺陷）。

## 方案详情

| 改动 | 位置 |
|------|------|
| 异常改传 `task_id` / `step_id`，删 `client_id` 读取 | [app/services/alarm/reporter.py](../../app/services/alarm/reporter.py) |
| `AlarmReportTask` 删 `client_id` 字段及 `from_dict` / `to_dict` 对应键 | [app/services/alarm/types.py](../../app/services/alarm/types.py) |
| 落库字典不再放 `client_id`；过闸后日志改打 `task` / `step` | [app/services/inference/online/temporal/alarm_sink.py](../../app/services/inference/online/temporal/alarm_sink.py) |
| 新增真实 reporter 走 `_report_with_retry` 的回归测试 | [tests/test_exception_handling.py](../../tests/test_exception_handling.py) `test_reporter_http_failure_is_retried` |

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 上报失败 | `TypeError`，CRITICAL，不重试 | 可重试 `PersistenceError`，退避 1s / 2s，共 3 次 |

**自测结果**

| 项 | 结果 |
|----|------|
| 新回归测试 | 修复前 FAILED，修复后 passed |
| 全量 `pytest tests/` | 983 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| KB 仍写「上报失败实际不重试」 | [SERVICE_ALARM.md](../kb/SERVICE_ALARM.md)、[DESIGN_FAULT_TOLERANCE.md](../kb/DESIGN_FAULT_TOLERANCE.md)、INDEX 描述需改回正常重试 | 下次 KB 融合时沉淀 |
