# health_monitor「decoder 活着但长期无帧」放弃路径补上身份 fence

> **变更状态**：生效中（2026-10-23）
> **知识库**：待沉淀

## 概述

`HealthMonitorWorker._check_all_clients` 中「decoder 进程活着、无帧超 `cleanup_timeout`」的放弃分支，改为直接以本轮 snapshot 的 CQ 作
`expected` 调拆除。修复前该分支走 `_exit_reconnect_mode(cleanup=True)`，而此时 task 不在重连表里，`expected` 恒为 `None`，fence 失效。

## 变更背景

- health_monitor「先决策后拿锁」：决策用的是本轮 snapshot 的 CQ，拿到任务锁之前 `/start` 可能已换代。
- 不带 `expected` 的 `stop_run` 会拆掉换代后的新 run（停 decoder、清数据）。其余三条 HM 拆除路径（重连失败、任务超时、孤儿流）都已带 fence，唯独此分支漏了。

## 方案详情

| 改动 | 落在哪 |
|------|--------|
| 放弃分支改为 `_stats["cleanups"] += 1; _cleanup_failed_client(task_id, expected=cq)` | [`app/daemons/health_monitor/worker.py`](../../app/daemons/health_monitor/worker.py) |

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 `test_alive_but_silent_give_up_carries_fence`（修复前失败、修复后通过） | passed |
| 全量 `pytest tests/` | 1120 passed |

## 遗留风险 / 后续任务

无。
