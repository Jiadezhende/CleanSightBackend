# recording 加取帧锁：sweeper 与拆除 flush 的「取走 + 入队」串行，段序不再倒置

> **变更状态**：生效中（2026-10-23）
> **知识库**：待沉淀

## 概述

`RecordingService` 新增进程级取帧锁 `_collect_lock`（RLock），`collect_from`（sweeper）与 `flush_residual`（拆除 / 断流）全程持有，
覆盖「从 CQ 取走 + 入 HLS / detections 队列」。修复拆除期 sweeper 与 `flush_residual` 并发取帧时段入队乱序的问题。

## 变更背景

- `stop_run` 在 ③ `flush_residual` 时 CQ 仍在注册表里（④ 才注销），sweeper 照常扫这个 DRAINING 的 CQ 并 `collect_from`。
- CQ 的取帧各自原子，但入队在 CQ 锁外：sweeper 先取走一整段、还没入队，拆除线程取走更晚的残帧并先入队。
  每段的 tfdt 按入队执行序累计，于是清单 ts 倒序、后写的段在媒体轴上盖掉先写的——画面丢一截，无报警。

## 方案详情

```text
recording 取帧锁（外层，RecordingService 自持）
  └─ CQ 锁 / _hls_inflight_lock（内层，各自方法内自取自放）
```

| 改动 | 落在哪 |
|------|--------|
| `_collect_lock = threading.RLock()`；`collect_from` / `flush_residual` 的取帧与入队全在锁内 | [`app/services/recording/service.py`](../../app/services/recording/service.py) |
| 模块不变式 2 改为「取帧必须持取帧锁，覆盖取走 + 入队」 | 同上 |
| sweeper docstring 去掉「别处直接调会竞态」的说法（串行已由 service 负责） | [`app/services/recording/sweep_worker.py`](../../app/services/recording/sweep_worker.py) |

- **可重入**：`collect_from` 的断流分支在锁内调 `flush_residual`。断流挂起机制（`_pending_flush`）本批不动。
- **生产方不取本锁**：decoder / 写回 / viz 只写 CQ，不受影响；争用只发生在拆除 flush 与 sweeper 撞车时，至多等一次 `collect_from`。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 新增 `TestCollectLock`：sweeper 取走整段卡在入队前时，拆除 flush 不得抢先入队（修复前失败、修复后通过） | passed |
| 全量 `pytest tests/` | 1119 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 队列满时 `submit` 最多阻塞 1s / 段，期间持取帧锁 | 录像队列过载时，拆除 flush 可能多等几秒（`/terminate` 响应变慢），不丢数据 | 观察；过载本身已有 `[recording]` 丢段告警 |
| 断流 flush 仍走挂起表，由 sweeper 执行 | 多一套挂起机制 | 下一批改为 health_monitor 直调 `flush_residual`，删挂起表 |
