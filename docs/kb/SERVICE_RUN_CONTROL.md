> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Run Control（运行编排）

`RunControlService` 是跨服务起停一次 run 的唯一编排出口（控制面）：ClientService 存 run，RunControlService 控 run 的起停。类在 `app/services/run_control/service.py`，单例 `run_control_service` 在 `instance.py`，`__init__.py` 只有 `lifespan()`（停机时拆 run，见下文）。

- **运行键 = int `task_id`**。`start_run` / `stop_run` 对称，全程持 `client_service.lock_for(task_id)`（per-task RLock）；api（经 `asyncio.to_thread`）与 HealthMonitorWorker（后台线程）共用这把锁。
- **CQ 的构造、`set`、`remove` 都在这里**：编排者分配 run、建 CQ，再交给各 owner。
- `service.py` 是单例引用面门禁里唯一允许模块级 import 各服务单例（client / inference / recording / stream）做跨域协调的文件；`instance.py` 不在放行之列。
- 本服务只按顺序驱动各 owner，不代持资源：decoder = StreamService，workflow / actor = InferenceService，HLS 残段与检测结果 = RecordingService，结算告警 = `alarm_sink`（过闸编排归 inference 域，alarm 只做无状态上报，见 [SERVICE_ALARM.md](SERVICE_ALARM.md)），注册表 = ClientService，run 目录 = `app.storage.runs`。

## start_run：锁外校验，锁内分配 run、建 CQ、起 workflow 与流

`start_run(task_id, current_step, rtsp_url, source_ip)`，入参全是 primitive，不碰 DB / HTTP。

1. **锁外校验，早于动旧 run**：字符串 `current_step`（DB `clean_task.current_step`）转 int `step_id`，非数字 → `ValidationError(field="current_step")`；`inference_service.resolve_stage(step_id)` 对未配置或无 detector 的 step 抛 `ValidationError`。二者都是 400，校验失败时同 task 在跑的旧 run 不受影响（`tests/test_start_rollback.py`）。

之后全程持 `lock_for(task_id)`：

2. **幂等或重启**：同 task 已有 run 时，`cq.run.step_id` 与 URL 都没变才幂等返回；任一变化就 `stop_run` 停旧、全量重建。
3. **分配 run**：`runs.allocate(task_id, step_id)` 建 `{task}/{step}/{run_id}/` 得 `RunIdentity`。必须在锁内（同 step 分配串行，`run_id` 才严格递增），且早于建 CQ。`OSError` → `AppError`（500；重启场景下此时旧 run 已停）。
4. **建 CQ**：`ClientQueues(run=…, source_ip=…, stage=…, **cq_kwargs())`，身份此后不可变。
5. **注册**：`client_service.set(task_id, cq)`。
6. **起 workflow**：`inference_service.start_workflow(cq)` 只建并启 Actor，不碰存储；返回 False 包成 `AppError`。
7. **起流**：`stream_service.start_stream(task_id, rtsp_url)`。

**6、7 任一步抛错即回滚**：`stop_run(task_id, expected=cq)` 对称拆除后重抛；`expected=cq` 防误清已被别的 `/start` 换上的新 CQ。已分配的 run 目录不回收（空目录随 step TTL 清）。

**换代不删旧产物**：新 run 写新目录，旧 run 的录像与检测结果保留到 step TTL，可按 `run_id` 点名回放。

## stop_run：尽力而为，固定顺序

`stop_run(task_id, reason, *, skip_decoder=False, expected=None)`。每步独立 try，单步失败不中断后续，永不抛出；错误收进返回值 `errors[]`（前缀 `decoder:` / `flush:` / `client_service:`）。

```text
0  身份 fence   expected 非空且当前槽位 is not expected → 整段放弃（result["skipped"]=True）
0b 封闸         cq.to_draining()：ACTIVE→DRAINING
1  停 decoder   stream_service.stop_stream(task_id)（异步 kill；skip_decoder=True 时跳过，用于孤儿流）
2  交出残余     settlement = inference_service.stop_workflow(cq)
               → alarm_sink.persist_alarms(settlement, cq=cq, mode=SETTLEMENT)
               → cq.set_latest_temporal([]) / set_latest_rendered(None)
               → recording_service.flush_residual(cq)：HLS 残段 + 剩余检测结果入队，回收本 cq 挂起的断流 flush
3  清注册表     expected 非空走 remove_if，否则 remove；cleanup=True 内含 cq.close()
```

- **`expected` 只由「先决策后拿锁」的调用方传**：HealthMonitorWorker 的自动结束路径与 start 回滚。HM 在 monitor 线程先决策、再拿锁，其间槽位可能被 `/start` 换成新 CQ；api 控制面持锁内决策并执行，没有 ABA，不传。
- **flush 必须早于步骤 3**：`cq.close()` 会释放帧。

## 进程停机：run_control 最先退出，逐个 stop_run

`run_control.lifespan()` 在 `app/main.py` 里嵌在最内层（inference 里层），无启动段；停机时最先退出，调 `RunControlService.shutdown()`：对 `client_service.snapshot()` 里每个 task 调 `stop_run(task_id, reason="shutdown")`，结算告警入 alarm 队列、HLS 残段与剩余检测结果入 recording 队列、注销 CQ。各层完整停机顺序见 [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)。

- **硬约束**：拆 run 必须在 `inference.stop()` 之前（actor 还在，结算走 `stop_run` 同一条路；之后 `inference.stop()` 的结算 Phase 2 只作兜底），且 recording / alarm 队列还活着——二者都在 inference 外层。
- run 的 decoder 与 `/terminate` 一样走 `stop_stream` 的异步 kill，之后的 `stream.shutdown()` 看不到它们。`decoder.stop()` 开头即 SIGKILL、毫秒级；最坏情况父进程先退，ffmpeg 写 stdout 时收 SIGPIPE 自行退出。
- 停机拆 run 不写 DB，唯一的外部副作用是结算告警上报（原本由 `inference.stop()` 上报，不重复）。
- 待核验：停机残段是否实际落盘，尚未在真实 RTSP 流上端到端验证（`docs/update/20260930_SHUTDOWN_STOP_RUNS.md` 遗留项）。

## 代码来源

- `app/services/run_control/{__init__,instance,service}.py`
- `app/main.py`（lifespan 嵌套）
- `app/storage/runs.py`（`allocate`）、`app/types/run.py`（`RunIdentity`）
- `app/services/client/service.py`（`lock_for` / `set` / `remove_if`）
- `app/services/inference/online/service.py`（`resolve_stage` / `start_workflow` / `stop_workflow` / `stop`）
- `app/services/inference/online/temporal/alarm_sink.py`（`persist_alarms`）
- `app/services/recording/service.py`（`flush_residual`）、`app/services/stream/service.py`（`start_stream` / `stop_stream`）
- `tests/test_teardown_identity_fence.py`、`tests/test_start_rollback.py`、`tests/test_api_concurrency.py`、`tests/test_run_control_shutdown.py`
