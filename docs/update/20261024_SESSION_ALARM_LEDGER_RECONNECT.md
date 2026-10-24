# 告警账本平移进 alarm_sink、`first_inference` 埋点下放写回、重连状态迁成 `RECONNECT_STATE`

> **变更状态**：生效中（2026-10-24）——session/mq 重构批 6
> **知识库**：待沉淀

## 概述

CQ 的告警账本（冷却闸门 + seq + 最近 100 条）原样抽成 `AlarmLedger`，由 `alarm_sink` 按 `Session` 弱引用挂表；`first_inference` 埋点由写回自行判断首次；
health_monitor 的 `_reconnecting_clients` 表换成每 run 的 `RECONNECT_STATE` topic，修掉「`/terminate` 正在重连的 run 后条目残留」。至此 CQ 只剩身份、状态与 `sess`。接口与日志不变。

## 变更背景

- **承接**：批 5b（[20261024_SESSION_MIGRATE_RECORDING_FEEDS](20261024_SESSION_MIGRATE_RECORDING_FEEDS.md)）后 CQ 只剩告警账本与一个启动埋点；health_monitor 仍有服务内的 task_id 表。
- 批 0（[20261023_RUN_LIFECYCLE_FIXES](20261023_RUN_LIFECYCLE_FIXES.md)）把「重连表剔除」推迟到本批：`/terminate` 一个正在重连的 run 后，表里条目残留，统计偏差，且该 task 被排除在孤儿 decoder 检查之外。

## 方案详情

### 全景

```text
actor tick / stop_run 结算 ──▶ alarm_sink.persist_alarms(alarms, sess=, mode=)
                                 sess CLOSED → 跳过；ledger_for(sess).record → 过闸才落库
GET /task/message            ──▶ ledger_for(sess).snapshot(since_seq)
GET /admin/clients/{id}/alarms ─▶ ledger_for(sess).recent(n)

health_monitor tick：每个 run 读 RECONNECT_STATE
  None 且 decoder 死 → set(ReconnectState) → flush 断流残帧（set 被拒 = run 已封闸，什么都不做）
  有状态            → 来新帧：再 flush 一次 → clear
                    → 超时：clear → stop_run(expected=cq)
                    → 仍死且到节流窗：set(replace(state, last_attempt_time)) → run_control.restart_stream(expected=cq)
```

| 改动 | 落在哪 |
|------|--------|
| `AlarmLedger`（CQ 账本代码原样平移，方法改名 `record` / `snapshot` / `recent`） | `app/services/inference/online/temporal/alarm_ledger.py` |
| `ledger_for(sess)`、`_ledgers` 弱引用表；`persist_alarms` 参数 `cq=` → `sess=` | `.../temporal/alarm_sink.py`；调用方 actor、run_control、inference 停机兜底 |
| `/task/message`、admin 告警读账本 | `app/routers/task.py`、`app/routers/admin.py` |
| 写回以「写前 `LATEST_DETECTION` 为空」判首次，打 `first_inference` | `detection/service.py` |
| `RECONNECT_STATE = Topic(Latest)`；`ReconnectState` 改 frozen、删 `cq` 字段 | `app/daemons/health_monitor/topics.py`、`types.py` |
| 重连状态读写改走 topic；`get_stats` / `get_system_status` 的重连列表现查 | `app/daemons/health_monitor/worker.py` |
| CQ 删账本、`_alarm_lock`、启动埋点 | `app/services/client/queues.py` |

- **账本写门不变**：只有 CLOSED 拒（拆除期结算告警仍入账），检查从 CQ 内挪到 sink 调用 `record` 之前，与旧实现一样在账本锁外。
- **账本的释放**：键是 `Session` 弱引用，session 对象回收即释放。旧实现在 `close()` 时清空；现在 close 后账本还在，直到最后一个持有该 session 的引用放手（actor 停、HM 本轮结束）。读方只经注册表拿 session，读不到已摘表 run 的账本。
- **重连 fence 简化**：旧实现在 `ReconnectState.cq` 里存进入重连时的 CQ，每 tick 核对槽位是否被 `/start` 换代。现在状态就挂在那个 run 上：换代后新 run 没有重连状态，按新 run 从头判定；旧 run 的状态随旧 run 关闭释放。respawn 与放弃时带的 `expected` 取本轮遍历到的 cq。
- **孤儿 decoder 检查**：重连状态挂在 run 上，没有 run 的 task 不可能在重连中，「跳过重连中的 task」一句随之删除。
- **进入重连时 run 已封闸**：`set` 被拒，直接返回，不 flush——拆除 flush 会收尾。旧实现在这个窗口里也会 flush 一次，两次 flush 由 recording 取帧锁串行，结果相同。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `/terminate` 正在重连的 run | `_reconnecting_clients` 条目残留，统计偏差，该 task 被排除在孤儿 decoder 检查外 | 重连状态随 run 关闭释放 |
| 服务内 task_id 表 | viz、health_monitor 各一张（viz 已在批 3 迁走） | 无；告警账本是挂 `Session` 弱引用的唯一显式例外 |
| CQ | 身份 + 状态 + 告警账本 + 埋点 | 身份 + 状态 + `sess` |

**自测结果**

| 项 | 结果 |
|----|------|
| 账本闸门 / seq / 增量（`test_alarm_increment`，改测 `AlarmLedger`）；sink 读别名、过闸、按 run 隔离、session 回收即释放（`test_alarm_sink`）；DRAINING 放行、CLOSED 拒（`test_cq_state_machine`） | passed |
| task 消息组装与接线（`test_task_message_*`） | passed |
| 重连：进入 / respawn / 成功 / 放弃 / 状态挂在 run 上 / 换代不串 / run 关闭即释放（`test_reconnect_on_initial_failure`） | passed |
| `first_inference` 每 run 一次（`test_writeback_handle_fence`） | passed |
| 全量 `pytest tests/` | 1224 passed |

## 遗留风险 / 后续任务

无（批 7 切注册表、写方句柄换成 `Session`、任务锁移入 run_control、删桥）。
