> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Run Control（运行编排）

`RunControlService`（`app/services/run_control/service.py`；单例 `run_control_service` 在 `instance.py`；`__init__.py` 纯 docstring、零 re-export）是**跨服务起停一次 run 的唯一编排出口**（控制面）。与 `ClientService`（存储 run）对仗：Registry 存 run，RunControlService 控 run 的起/停。

**运行键 = `task_id`(int)**。`start_run` / `stop_run` 对称，均在 `client_service.lock_for(task_id)`（per-task RLock）下串行——api（经 `asyncio.to_thread`）与 HealthMonitorWorker（后台线程）共用同一把锁，消除竞态。CQ 的**构造职责在此**（编排者分配 run、建 CQ → `start_workflow(cq)`）；`service.py` 是门禁允许模块级 import 各服务单例（client / inference / recording / stream）做跨域协调的居所（`instance.py` 不在放行之列）。

## start_run(task_id, current_step, rtsp_url, source_ip)

入参均为 primitive（不接触 DB/HTTP）。

1. **参数校验（锁外、动旧 run 之前）**：字符串 `current_step`（DB `clean_task.current_step`）一次转换为 int `step_id`，非数字 → `ValidationError(field="current_step")`（400）；`stage = inference_service.resolve_stage(step_id)`，推理配置未定义该 step 或无 detector → `ValidationError`（400）。校验失败时不调 `stop_run`，同 task 已在跑的旧 run 不受影响（`tests/test_start_rollback.py`）。

之后全程持 `lock_for(task_id)`：

2. **幂等 / 重启清理**：同 task 已有 run 时，`cq.run.step_id` 与 URL 均未变才幂等返回；任一变化则 `stop_run` 停旧后全量重建。
3. **分配 run**：`runs.allocate(task_id, step_id)` 建 `{task}/{step}/{run_id}/`，得到 `RunIdentity`。**必须在 `lock_for` 内**（同 step 的分配串行，`run_id` 才严格递增）且**早于 CQ 构造与 `client_service.set`**（CQ 构造时即带上 `RunIdentity`，此后不可变）。`OSError` → `AppError`（500：start 会碰盘；重启场景下分配失败时旧 run 已停）。
4. **建 CQ**：`ClientQueues(run=run, source_ip=…, stage=…, **cq_kwargs())`，身份不可变。
5. **注册 CQ**：`client_service.set(task_id, cq)`（COW 发布）。**CQ 的 set/remove 均归 RunControlService**，与 `stop_run` 的 `client_service.remove` 对称（set 先、remove 后，镜像）。set 后的 setup 步（6、7）包进一个 `try`：任一步失败即回滚注销，避免 CQ 泄漏在注册表。
6. **起 workflow**：`inference_service.start_workflow(cq)` 只建并启 Actor，**不碰存储**（start 侧没有任何存储钩子）。
7. **起流**：`stream_service.start_stream(task_id, rtsp_url)`（decoder 键 = task_id；系统只用 RTSP）。

**换代不删旧产物**：新 run 写新目录，旧 run 的录像与检测结果保留到 step TTL，可按 `run_id` 点名回放。

**失败回滚**：6/7 任一步抛错（`start_workflow` 返回 False 时包成 `AppError`）→ `stop_run(task_id, expected=cq)` 对称回滚（尽力而为停 decoder/actor、交出残余产物、`client_service.remove` 注销 CQ），再重抛。`expected=cq` 做身份 fence，防误清已被 `/start` 换上的健康新 CQ。分配出的 run 目录不回收（空目录随 step TTL 清）。

## stop_run(task_id, reason, *, skip_decoder, expected)

拆除一次 run，**尽力而为**：每步独立 try、单步失败不中断后续、永不抛出（错误收进返回值 `errors[]`，前缀 `decoder:` / `flush:` / `client_service:`）。固定顺序：

0. **对象身份 fence**（`expected` 仅 HealthMonitorWorker 自动结束路径与 start 回滚传）：HM 在 monitor 线程「先决策后拿锁」，决策→拿锁之间槽位可能被 `/start` 重启换新 CQ；若当前槽位已非 `expected`，整段放弃，防误删健康新 run。api 控制面持锁内决策+执行、无 ABA，不传 expected。
0b. **封闸** `cq.to_draining()`：ACTIVE→DRAINING，封生产者写；迟到写被门拒不串台，settlement 告警 + 残余 flush 仍放行。
1. **停 decoder**：`stream_service.stop_stream(task_id)`（`skip_decoder=True` 用于孤儿流）。
2. **落盘残余**（按 owner 归位）：`inference_service.stop_workflow(cq)` 停 actor 并交出 settlement 告警 → `alarm_sink.persist_alarms(settlement, cq, mode=SETTLEMENT)`（别名已由 actor 烧进 `alarm.stage`）→ 清前端槽（`set_latest_temporal([])` / `set_latest_rendered(None)`）→ `recording_service.flush_residual(cq)` 交出 HLS 残段与剩余检测结果，并回收本 cq 挂起的断流 flush 请求。
   > **flush 必须早于步骤 3**：步骤 3 的 registry.remove 会 `cq.close()` 释放帧。
3. **清 registry**：传 `expected` 走 `remove_if`（身份 fence 删除），否则 `remove`；`cleanup=True` 内含 `cq.close()`（置 CLOSED + 释放 payload）。

## 职责边界

- **告警落库归属**：过闸编排（`persist_alarms`）在 `inference/online/temporal/alarm_sink`，alarm 服务只做无状态上报。RunControlService 只在拆除结算点调用 `alarm_sink.persist_alarms`（别名已由 actor 构造期一次性烧进 `alarm.stage`，上报直读 `alarm.stage` / `alarm.metric`）。
  > 边界固定：告警过闸/编排归属 inference 域（`inference/online/temporal/`），**不迁入 alarm**；alarm 只做无状态上报（见 [SERVICE_ALARM.md](SERVICE_ALARM.md)）。
- **owner 清晰**：decoder = StreamService、workflow/actor = InferenceService、HLS 残段与检测残余 = RecordingService、告警上报 = AlarmService、registry = ClientService、run 目录分配 = `app.storage.runs`（由本服务在锁内调用）。RunControlService 只按顺序驱动各 owner，不代持其资源。

## 代码来源

- `app/services/run_control/{__init__,instance,service}.py`
- `app/storage/runs.py`（`allocate`）、`app/types/run.py`（`RunIdentity`）
- `app/services/client/service.py`（`lock_for` / `set` / `remove_if`）、`app/services/client/queues.py`（`ClientQueues(run=…)`）
- `app/services/inference/online/instance.py`、`app/services/inference/online/service.py`（`start_workflow` / `stop_workflow` / `resolve_stage`）
- `app/services/inference/online/temporal/alarm_sink.py`（`persist_alarms`）
- `app/services/recording/service.py`（`flush_residual`）、`app/services/recording/instance.py`
- `app/services/stream/service.py`（`start_stream` / `stop_stream`）
- `tests/test_teardown_identity_fence.py`、`tests/test_start_rollback.py`、`tests/test_api_concurrency.py`
