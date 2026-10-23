# run 生命周期先行修复：`/start` 幂等、拆除落盘、录像取帧串行、health_monitor 生命周期写统一经 run_control

> **变更状态**：生效中（2026-10-23）
> **知识库**：待沉淀

## 概述

修复 run 启停与断流链路上的 6 个现网 bug，并做 1 项随之可行的简化，共 7 处改动，各自一个提交：
`/start` 重复调用不再拆分 run；拆除时结算失败不再丢残段；录像各取帧路径串行，段序不再倒置；health_monitor 的拆除、重启流、停孤儿 decoder 一律经 run_control 持任务锁并核对 fence；
断流残帧改由 health_monitor 直接 flush，删除 recording 的挂起 flush 机制。对外接口、配置、日志与指标名不变。

## 变更背景

- **现状**：一次 run 的启停由 `RunControlService` 持 per-task 锁（`client_service.lock_for(task_id)`）编排；health_monitor 在后台线程「先决策后拿锁」，
  靠对象身份 fence（`expected=cq`，拿锁后核对注册表里仍是决策时捕获的 CQ，否则放弃）防止误动被 `/start` 换代后的新 run。
  recording 的 sweeper 每秒从 CQ 取整段落盘，拆除与断流时另有 flush 残帧的路径。
- **触发来源**：session/mq 重构提案评审时梳理出的先行修复项，与重构无依赖，先行落地。

| 编号 | 问题 | 后果 |
|------|------|------|
| #1 | `start_run` 幂等判断拿原始入参 URL 比 decoder 记录的 URL，而后者是改写过的内部地址（代理端口 → `127.0.0.1:内部端口`） | 入参走代理端口时永远判不等：同一 URL 重复 `/start` 会停旧 run、另开 run。一次流程被拆成多个 run，离线判读只取最新 run，前段录像与检测结果不参与判读 |
| #2 | `stop_run` 的结算告警、清前端槽、残段落盘共用一个 try | 结算告警上报抛错 → 残段与剩余检测结果不落盘就被 `cq.close()` 释放 |
| #3 | 拆除期 CQ 仍在注册表，sweeper 照常扫这个 DRAINING 的 CQ，与拆除 flush 并发取帧；取帧各自原子，入队却在 CQ 锁外 | 后取的段先入队 → 每段 tfdt 按入队序累计，清单 ts 倒序、后写的段盖掉先写的，画面丢一截，无报警 |
| #4 | 「decoder 活着但长期无帧」放弃分支走 `_exit_reconnect_mode(cleanup=True)`，此时 task 不在重连表里，`expected` 恒为 None | fence 失效：`/start` 恰好换代时拆掉新 run |
| #5 | 停孤儿 decoder（有 decoder 无 CQ）时持锁后直接 `stop_stream`，不复查 | 判孤儿到拿锁之间 `/start` 已建好 CQ 与新 decoder，新 run 的 decoder 被停，run 收不到帧直至超时拆除 |
| #6 | 重连 respawn 直调 `stream_service.restart_stream`，不持任务锁 | 换代后先停掉新 run 的 decoder，再用重连状态里的旧 URL 起 decoder 并按 task_id 绑到新 CQ：新 run 从旧地址拉流，无报警 |
| #7 | （非 bug）断流残帧为避开「第二个取帧者」绕成挂起表：health_monitor 登记 → sweeper 下一 tick 执行 → 拆除时按身份回收 | 三个线程共用一张免锁字典，需额外的身份核对与回收逻辑 |

## 方案详情

### 全景：改动落在生命周期的哪一步

```text
/start ── start_run（持任务锁）
            幂等判断 ............................................. #1
            分配 run → 建 CQ → start_workflow → 起流

health_monitor（锁外决策，携带决策时捕获的 cq）── run_control（持任务锁执行，先核对 fence）
  decoder 进程死 → 进入重连 → flush 断流前残帧 ......................... #7
                 → 节流 respawn ──▶ restart_stream(expected=cq) ........ #6
                 → 来新帧 → 同一栅栏再 flush 一次 ...................... #7
                 → 超时 ──▶ stop_run(expected=cq)
  decoder 活着但长期无帧 ──▶ stop_run(expected=cq) ...................... #4
  有 decoder 无 CQ ──▶ stop_orphan_decoder（锁内复查无 run） ............ #5

stop_run（持任务锁）
  封闸 DRAINING → 停 decoder
  → ① 结算告警  ② 清前端槽  ③ flush 残帧（三步各自 try） ................ #2
  → 注销 CQ（close 释放帧）

recording 取帧锁：sweeper / 断流 flush / 拆除 flush 的「取走 + 入队」串行 .... #3
```

硬约束：

- **health_monitor 只决策，生命周期写只走 run_control**：拆除、重启流、停孤儿 decoder 都在 run_control 持任务锁时执行，执行前核对 fence。
- **取帧必须持 recording 取帧锁，且覆盖「取走 + 入队」**：三条取帧路径都从 CQ 队头取，先取者先入队，入队序即 ts 序。

| 编号 | 改动 | 落在哪 | 提交 | 详见 |
|------|------|--------|------|------|
| #1 | 幂等判断前对入参做同一改写 | `run_control/service.py`、`stream/service.py` | `8174095` | §1 |
| #2 | `stop_run` 三步独立 try | `run_control/service.py` | `0ccb227` | §2 |
| #3 | recording 取帧锁 | `recording/service.py` | `4e089fa` | §3 |
| #4 | 放弃分支带 fence | `health_monitor/worker.py` | `817f86b` | §4 |
| #5 | `run_control.stop_orphan_decoder` | `run_control/service.py`、`health_monitor/worker.py` | `10c030b` | §5 |
| #6 | `run_control.restart_stream` | `run_control/service.py`、`health_monitor/worker.py` | `686e18d` | §6 |
| #7 | 断流直接 flush，删挂起机制 | `recording/service.py`、`health_monitor/worker.py` | `95df2b8` | §7 |

### 1. `/start` 幂等判断比较同形 URL

- [`app/services/stream/service.py`](../../app/services/stream/service.py) 新增 `internal_stream_url(url)`：按 settings 套用代理端口改写。
  起流（`_start_stream_impl`）与幂等比较共用它，改写规则只有一处。
- [`app/services/run_control/service.py`](../../app/services/run_control/service.py)：`cur_url == rtsp_url` → `cur_url == internal_stream_url(rtsp_url)`。

### 2. `stop_run` 的结算、清前端槽、残段落盘各自 try

| 步骤 | 失败时 |
|------|--------|
| ① `stop_workflow` + `persist_alarms`（结算告警） | 记 `settlement: …`，继续 |
| ② 清前端槽（最新时序结果、最新渲染帧） | 记 `frontend: …`，继续 |
| ③ `recording_service.flush_residual(cq)` | 记 `flush: …`；成功才置 `data_flushed` |

顺序不变，③ 仍早于注销 CQ。`data_flushed` 现在只反映 ③ 是否成功。

### 3. recording 取帧锁

```text
recording 取帧锁（外层，RecordingService 自持）
  └─ CQ 锁 / 在途字节锁（内层，各自方法内自取自放）
```

- `RecordingService._collect_lock`，`collect_from`（sweeper）与 `flush_residual`（拆除 / 断流）全程持有，覆盖取帧与入 HLS / detections 队列。
- 只锁「取走」不够：入队在 CQ 锁外，必须连入队一起锁住。
- 生产方（decoder、推理写回、viz）不取本锁；争用只发生在 flush 与 sweeper 撞车时，至多等一次 `collect_from`。
- 模块不变式 2 改为「取帧必须持取帧锁，覆盖取走 + 入队」。

### 4. 「decoder 活着但长期无帧」放弃分支带 fence

[`app/daemons/health_monitor/worker.py`](../../app/daemons/health_monitor/worker.py)：该分支改为 `_stats["cleanups"] += 1; _cleanup_failed_client(task_id, expected=cq)`，
`cq` 取本轮 snapshot。至此 health_monitor 四条拆除路径（重连失败、无帧超时、任务超时、孤儿流）都带 fence。

### 5. 停孤儿 decoder 经 run_control，锁内复查

- `RunControlService.stop_orphan_decoder(task_id) -> bool`：`lock_for` 内 `has_client(task_id)` 为真即放弃返回 False，否则 `stop_stream` 返回 True。
  `start_run` 全程持同一把锁，锁释放时 CQ 与新 decoder 已同时就位，复查即可区分「真孤儿」与「刚起的新 run」。
- `HealthMonitorWorker` 增 `run_control` 入参（第六个可注入协作者，缺省时 `_resolve_deps` 现取单例）；`_handle_orphan_decoder` 改调它，`cleanup_client` 也改用注入的引用调 `stop_run`。
- [`tests/test_import_hygiene.py`](../../tests/test_import_hygiene.py) 的单例例外说明同步为「五个协作者，生命周期写一律经 run_control」。

### 6. 重连 respawn 经 run_control 核对 fence

- `RunControlService.restart_stream(task_id, stream_url, *, expected) -> bool`：`lock_for` 内注册项不是 `expected` 即放弃返回 False，否则调 `stream_service.restart_stream`。
- health_monitor respawn 改调它，`expected` 取进入重连时捕获的 CQ（`ReconnectState.cq`）。
- stream 侧签名不变：持锁期间注册项不变，`_restart_stream_impl` 按 task_id 取到的 CQ 即核对过的那一个；其 docstring 注明「只由 run_control 持任务锁调用」。
- `_restart_stream_impl` 同步停旧 decoder（kill + reap，ms 级）现发生在任务锁内，`/start`、`/terminate` 至多多等这几 ms。

### 7. 断流残帧由 health_monitor 直接 flush，删除挂起机制

依赖 §3：有了取帧锁，任意线程直接 flush 都不会让段序倒置，挂起机制存在的理由不再成立。

| 时机 | 旧 | 新 |
|------|----|----|
| 进入重连 | `request_residual_flush(cq, fence_ts=断流前最后一帧 ts)` 登记 | `flush_residual(cq, until_ts=同上)` 当场执行 |
| 重连成功 | 同一栅栏再登记一次 | 同一栅栏再 flush 一次（捞迟到的断流前 processed 帧） |
| sweeper 每 tick | 整段 → 取挂起请求 → 残段 → detections | 整段 → detections |
| 拆除 | 全排空 + 按身份回收挂起请求 | 全排空 |

- 删除 `request_residual_flush`、`_pending_flush`、`_take_pending_flush`；取帧锁不再需要可重入，RLock 改为 Lock。
- **段序仍对**：三条路径都从队头取。断流 flush 先拿到锁就把栅栏前的帧（含本可凑成整段的部分）按段长切完入队，sweeper 之后只能取到更晚的帧；反之亦然。
  旧版「残段必须排在本轮整段之后」的顺序约束随之消失。
- **换代**：health_monitor 传的是进入重连时捕获的 CQ；换代后旧 CQ 已 CLOSED，drain 取到空、什么都不提交，无需身份核对。
- 同步改 `recording/__init__.py`、`recording/instance.py`、`sweep_worker.py` 的说明。

### 8. 保留项（不改动）

- `stop_run` 的编排顺序（封闸 → 停 decoder → 结算 → 清槽 → flush → 注销）与回滚语义。
- 断流 flush 的栅栏取值（断流前最后一帧 ts）与「进入重连、重连成功各 flush 一次」的时机。
- `stream_service.restart_stream` / `stop_stream` 的签名与行为。
- health_monitor `_reconnecting_clients` 的维护方式（`/terminate` 正在重连的 run 后条目会残留，见遗留风险）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 同一 URL 重复 `/start`（代理端口） | 每次停旧 run、另开 run | 幂等返回 |
| 结算告警失败时的残段 | 丢 | 照常落盘 |
| 拆除期 sweeper 与 flush 并发 | 段可能倒序入队，画面丢一截 | 先取先入队，段序 = ts 序 |
| health_monitor 生命周期写 | 部分直调 stream、部分无 fence | 全部经 run_control 持任务锁，执行前核对 fence |
| 断流残帧交接 | 挂起表，三线程共用 | 当场 flush，`RecordingService` 净减约 100 行 |

**自测结果**

每项修复都先写复现用例，确认修复前失败、修复后通过。

| 项 | 用例 | 结果 |
|----|------|------|
| #1 | `test_api_concurrency::test_same_proxy_url_is_idempotent` | passed |
| #2 | `test_run_control_shutdown::test_stop_run_flushes_residual_even_if_settlement_fails` | passed |
| #3 / #7 | `test_recording_service::TestCollectLock`（拆除、断流栅栏两种 flush：sweeper 取走整段卡在入队前时，flush 不得抢先入队） | passed |
| #4 | `test_reconnect_on_initial_failure::test_alive_but_silent_give_up_carries_fence` | passed |
| #5 | `test_teardown_identity_fence`：有 run 时放弃 / 仍为孤儿时停；health_monitor 侧经 `run_control.stop_orphan_decoder`、不直调 stream | passed |
| #6 | `test_teardown_identity_fence`：fence 未命中放弃 / 命中重启；`test_full_reconnect_state_machine` 断言经 `run_control.restart_stream` 且带 `expected` | passed |
| #7 | health_monitor 两处 flush 改断言 `flush_residual(cq, until_ts=...)`；删除挂起机制专属用例 9 条 | passed |
| 全量 `pytest tests/` | — | 1117 passed |
| dev 环境真流端到端（断流重连、拆除） | 人工 | 通过（2026-10-23） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 录像队列满时 `submit` 每段最多阻塞 1s，期间持取帧锁；断流 flush 现跑在 health_monitor 线程 | 过载时 `/terminate` 或一轮健康检查推迟几秒，不丢数据 | 观察；过载本身已有 `[recording]` 丢段告警 |
| `/terminate` 正在重连的 run 后，`_reconnecting_clients` 条目残留 | 统计偏差；该 task 被排除在孤儿 decoder 检查外（#6 后不再因 respawn 竞态产生孤儿） | 随 session 重构用每 run 的重连状态替代该表；不重构则补每 tick 剔除 |
