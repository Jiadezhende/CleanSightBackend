# 重连 respawn 改经 run_control 持任务锁核对 fence，不再误动换代后的新 run

> **变更状态**：生效中（2026-10-23）
> **知识库**：待沉淀

## 概述

新增 `RunControlService.restart_stream(task_id, stream_url, *, expected)`：持任务锁核对注册项仍是 `expected` 才调 `stream_service.restart_stream`。
health_monitor 重连 respawn 改调它，`expected` 取进入重连时捕获的 CQ。至此 health_monitor 的生命周期写（拆除、重启流、停孤儿 decoder）全部经 run_control。

## 变更背景

- health_monitor 每 tick 开头核对一次 fence（`cq is not state.cq` 即放弃），但随后的 `stream_service.restart_stream` **不持任务锁**。
- 核对到 respawn 之间 `/start` 若换代：`_restart_stream_impl` 先停掉 decoders 表里的 decoder（已是新 run 的），
  再用重连状态里的**旧 URL** 起一个 decoder、按 task_id 绑到**新 CQ** 上。新 run 从旧地址拉流，且无报警。

## 方案详情

```text
health_monitor（决策，锁外）                 run_control（执行，持 lock_for(task_id)）
  进入重连：捕获 state.cq                    restart_stream(task_id, url, expected=state.cq)
  每 tick：进程仍死 + 到节流窗 ───────────▶    注册项 is not expected → 放弃，返回 False
                                               否则 stream_service.restart_stream（锁内取到的 CQ 即 expected）
```

| 改动 | 落在哪 |
|------|--------|
| `restart_stream(task_id, stream_url, *, expected) -> bool` | [`app/services/run_control/service.py`](../../app/services/run_control/service.py) |
| respawn 改调 `self._run_control.restart_stream(..., expected=state.cq)` | [`app/daemons/health_monitor/worker.py`](../../app/daemons/health_monitor/worker.py) |
| `stream_service.restart_stream` docstring 注明「只由 run_control 持任务锁调用」 | [`app/services/stream/service.py`](../../app/services/stream/service.py) |

- stream 侧签名不变：持锁期间注册项不会变，`_restart_stream_impl` 按 task_id 取到的 CQ 就是核对过的那一个，无需额外传参。
- `_restart_stream_impl` 内同步停旧 decoder（kill + reap，ms 级）发生在任务锁内；`/start`、`/terminate` 与之串行，至多等这几 ms。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 run_control 侧：fence 未命中放弃、命中重启（`test_teardown_identity_fence.py`） | passed |
| `test_full_reconnect_state_machine` 改为断言经 `run_control.restart_stream` 且带 `expected`（修复前失败） | passed |
| 全量 `pytest tests/` | 1125 passed |

## 遗留风险 / 后续任务

无。
