> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Run Control（运行编排）

`RunController`（`app/services/run_control.py`，单例 `run_controller`）是**跨服务起停一次 run 的唯一编排出口**（控制面）。与 `ClientManager`（存储 run）对仗：Registry 存 run，Controller 控 run 的起/停。

**运行键 = `task_id`(int)**。`start_run` / `stop_run` 对称，均在 `client_manager.lock_for(task_id)`（per-task RLock）下串行——api（经 `asyncio.to_thread`）与 HealthMonitor（后台线程）共用同一把锁，消除竞态。CQ 的**构造职责在此**（编排者建 CQ → `start_workflow(cq)`）；这里是允许直接 import 各服务单例做跨域协调的居所（软约定：跨域 import 尽量收敛于此）。

## start_run(task_id, current_step, rtsp_url, source_ip)

入参均为 primitive（不接触 DB/HTTP）。边界层把字符串 `current_step`（DB `clean_task.current_step`，恒为数字串）一次转换为 int `step_id`（非数字属坏数据 → `int()` 抛错走 api 异常处理）。全程持 `lock_for(task_id)`：

1. **幂等 / 重启清理**：同 task 已有 run 时，`step_id` 与 URL 均未变才幂等返回；任一变化则 `stop_run` 停旧后全量重建。
2. **建 CQ**：`stage = inference_manager.resolve_stage(current_step)`，构造不可变身份 CQ（含 `source_ip` 被动字段）。
3. **storage supersede**（与 stop 侧对称的两个 service 钩子）：`persistence_manager.start_run(cq)` 清空旧 HLS step 目录（无 owner，纯 rmtree）；`inference_manager.start_workflow(cq)` 内含 `FeatureStore.open_fresh`（认领 owner + 截断旧 `features.jsonl`）。均在建新 CQ 之后、无活跃 worker 写该 `(task,step)` 之前完成。
4. **起流**：`stream_service.start_stream(task_id, rtsp_url)`（decoder 键 = task_id；系统只用 RTSP）。

`start_workflow` 失败抛 `AppError`。

## stop_run(task_id, reason, *, skip_decoder, expected)

拆除一次 run，**尽力而为**：每步独立 try、单步失败不中断后续、永不抛出。固定顺序：

0. **对象身份 fence**（`expected` 仅 HealthMonitor 自动结束路径传）：HM 在 monitor 线程「先决策后拿锁」，决策→拿锁之间槽位可能被 `/start` 重启换新 CQ；若当前槽位已非 `expected`，整段放弃，防误删健康新 run。api 控制面持锁内决策+执行、无 ABA，不传 expected。
0b. **封闸** `cq.to_draining()`：ACTIVE→DRAINING，封生产者写；迟到写被门拒不串台，settlement 告警 + HLS flush 仍放行。
1. **停 decoder**：`stream_service.stop_stream(task_id)`（`skip_decoder=True` 用于孤儿流）。
2. **落盘残余**（按 owner 归位）：`inference_manager.stop_workflow(cq)` 停 actor + 关 feature 分区并交出 settlement 告警 → `alarm_sink.persist_alarms(settlement, cq, mode=SETTLEMENT)`（别名已由 actor 烧进 `alarm.stage`）→ 清前端槽（`set_latest_temporal([])` / `set_latest_rendered(None)`）→ `persistence_manager.flush_residual_segments(cq)` 落 HLS 残段。
3. **清 registry**：传 `expected` 走 `remove_if`（身份 fence 删除），否则 `remove`；`cleanup=True` 内含 `cq.close()`（置 CLOSED + 释放 payload）。
4. **回收 HLS 目录锁**：`persistence_manager.release_task_locks(task_id)`。

## 职责边界

- **告警落库归属**：过闸编排（`persist_alarms`）在 `inference/temporal/alarm_sink`，persistence 只做无状态落库。RunController 只在拆除结算点调用 `alarm_sink.persist_alarms`。
- **owner 清晰**：decoder=StreamService、workflow/actor/feature=InferenceManager、HLS 残段/目录锁=PersistenceManager、registry=ClientManager。RunController 只按顺序驱动各 owner，不代持其资源。

## 代码来源

- `app/services/run_control.py`
- `app/services/client/manager.py`（`lock_for` / `set` / `remove_if`）
- `app/services/inference/instance.py`、`app/services/inference/manager.py`（`start_workflow` / `stop_workflow` / `resolve_stage`）
- `app/services/inference/temporal/alarm_sink.py`（`persist_alarms`）
- `app/services/persistence/manager.py`（`start_run` / `flush_residual_segments` / `release_task_locks`）
- `app/services/stream/service.py`（`start_stream` / `stop_stream`）
- `tests/test_teardown_identity_fence.py`
