# 停孤儿 decoder 改经 run_control，锁内复查无 run 才停

> **变更状态**：生效中（2026-10-23）
> **知识库**：待沉淀

## 概述

新增 `RunControlService.stop_orphan_decoder(task_id)`：持任务锁复查注册表，该 task 已有 CQ 则放弃，否则停 decoder。
health_monitor 的孤儿 decoder 路径改调它；同时把 `run_control` 作为第六个可注入协作者（`_resolve_deps` 现取），`cleanup_client` 也改经该引用调 `stop_run`。

## 变更背景

- health_monitor 在锁外按 snapshot 判定「有 decoder 无 CQ」，原先持 `lock_for` 后直接 `stop_stream`，**没有复查**。
- 判定到拿锁之间 `/start` 可能已为该 task 建好 CQ 与新 decoder（`start_run` 全程持同一把锁，释放时两者都已就位），
  此时 health_monitor 停掉的是新 run 的 decoder：run 还在注册表里，却再也收不到帧，直到无帧超时被拆除。

## 方案详情

| 改动 | 落在哪 |
|------|--------|
| `stop_orphan_decoder(task_id) -> bool`：`lock_for` 内 `has_client` 为真即返回 False，否则 `stop_stream` | [`app/services/run_control/service.py`](../../app/services/run_control/service.py) |
| `HealthMonitorWorker` 增 `run_control` 入参（缺省时 `_resolve_deps` 取单例）；`_handle_orphan_decoder` 改调 `stop_orphan_decoder`；`cleanup_client` 改用注入的引用 | [`app/daemons/health_monitor/worker.py`](../../app/daemons/health_monitor/worker.py) |
| 单例例外说明同步为「五个协作者，生命周期写一律经 run_control」 | [`tests/test_import_hygiene.py`](../../tests/test_import_hygiene.py) |

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 run_control 侧：有 run 时放弃 / 仍为孤儿时停（`test_teardown_identity_fence.py`） | passed |
| 新增 HM 侧：孤儿 decoder 经 `run_control.stop_orphan_decoder`、不直调 stream | passed |
| 全量 `pytest tests/` | 1123 passed |

## 遗留风险 / 后续任务

无。
