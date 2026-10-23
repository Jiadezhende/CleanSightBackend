# 断流残帧改由 health_monitor 直接 flush，删除 recording 的挂起 flush 机制

> **变更状态**：生效中（2026-10-23）
> **知识库**：待沉淀

## 概述

health_monitor 在断流（进入重连）与重连成功两处，改为直接调 `recording_service.flush_residual(cq, until_ts=fence)`。
recording 删除 `request_residual_flush`、`_pending_flush`、`_take_pending_flush`，以及 `collect_from` 的断流分支和 `flush_residual` 的拆除回收分支；
取帧锁从 RLock 改为 Lock。落盘行为不变：仍按「断流前最后一帧 ts」切残段，段序仍等于 ts 序。

## 变更背景

- **承接**：建立在上一批「recording 取帧锁」之上。该锁让 sweeper、拆除 flush 的「取走 + 入队」串行，任意线程直接 flush 都不会让段序倒置。
- 原先禁止 health_monitor 就地 flush，理由是「会多出第二个 drain 者、入队乱序」，因此绕成一套挂起表：health_monitor 登记 → sweeper 下一 tick 执行 → 拆除时按身份回收。
  这套机制有三个线程同碰一张免锁字典，还要靠对象身份核对防换代串台。有了取帧锁，这个理由不再成立。

## 方案详情

```text
断流（进入重连）      health_monitor ── flush_residual(cq, until_ts=断流前最后一帧 ts) ─┐
重连成功（同一栅栏）  health_monitor ── flush_residual(cq, until_ts=同上) ──────────────┤
每 tick              sweeper ──────── collect_from(cq)（只取整段 + detections） ───────┼─ 取帧锁串行：先取者先入队
拆除                  run_control ──── flush_residual(cq)（全排空） ──────────────────┘
```

| 改动 | 落在哪 |
|------|--------|
| 删 `request_residual_flush` / `_pending_flush` / `_take_pending_flush`；`collect_from` 只取整段与 detections；`flush_residual` 去掉回收分支；取帧锁改 `threading.Lock` | [`app/services/recording/service.py`](../../app/services/recording/service.py) |
| 两处登记改为直接 `flush_residual(cq, until_ts=...)` | [`app/daemons/health_monitor/worker.py`](../../app/daemons/health_monitor/worker.py) |
| 包 docstring / 单例说明 / 门禁注释同步去掉 `request_residual_flush` | `recording/__init__.py`、`recording/instance.py`、`sweep_worker.py`、`tests/test_import_hygiene.py` |

- **为什么段序仍对**：三条路径都从 CQ 队头取帧。断流 flush 先拿到锁，就把栅栏前的帧（含本可凑成整段的部分）按段长切完入队；
  sweeper 后拿到锁，只能取到更晚的帧。反过来亦然。不再需要旧版「残段必须排在本轮整段之后」这条顺序约束。
- **换代**：health_monitor 传的是进入重连时捕获的 CQ；换代后旧 CQ 已 CLOSED，drain 取到空，什么都不提交，不需要身份核对。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 断流残帧交接 | 登记挂起表 → sweeper 下一 tick 执行 → 拆除时回收 | health_monitor 当场 flush |
| 碰挂起表的线程 | 3 个（免锁，靠 dict 单操作原子） | 无此表 |
| `RecordingService` 代码 | — | 净减约 100 行 |

**自测结果**

| 项 | 结果 |
|----|------|
| `TestCollectLock` 增「断流栅栏」参数化：sweeper 取走整段卡在入队前时，断流 flush 不得抢先入队 | passed |
| health_monitor 两处 flush 断言改为 `flush_residual(cq, until_ts=...)`（一次 / 同栅栏两次 / 缺 stream_info 不调） | passed |
| 删除挂起机制专属用例 9 条 | — |
| 全量 `pytest tests/` | 1117 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| flush 现在跑在 health_monitor 线程，录像队列满时 `submit` 每段最多阻塞 1s | 队列过载时该轮健康检查推迟几秒；不丢数据 | 观察；过载本身已有 `[recording]` 丢段告警 |
