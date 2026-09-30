> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Client State Service

`ClientService`（注册表）+ `ClientQueues`（下称 CQ，一次 run 的共享状态）是流、推理、可视化、告警、录制之间的共享状态层，包 `app/services/client/`（`__init__.py` 纯 docstring，单例 `client_service` 在 `instance.py`）。跨服务起停编排不在本层，归 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)。

- **运行键 = int `task_id`**；落盘按 `cq.run`（`RunIdentity`）定位 run 目录。`source_ip` 是被动来源字段，不是运行键。
- **零跨服务依赖的 leaf**：CQ 只吐自有词汇（流名 = detector.name、内部 primitive），「流名 → metric / 中文标签 / stage 别名」的映射在 router 装配层做。因此 `client_service` 不在单例引用面门禁（`SINGLETONS`）里，谁都可以向下依赖它。

## ClientService：COW 注册表 + per-task 事务锁

注册表是不可变 dict `_runs: {task_id → CQ}`：读者原子读引用后免锁迭代，写者在 `_wlock` 下复制—改—换引用。本类只做哑存储，不构造 CQ。

- 读（无锁）：`get`、`has_client`、`snapshot()`（零拷贝只读视图）、`find_by_source_ip`、`get_all_queue_depths`、`get_status_summary`。写（`_wlock` + COW）：`set`、`remove(cleanup=True)`、`remove_if`、`clear_all`；`cleanup=True` 在锁外调 `cq.clear()`（= `close()`）。
- **`remove_if(task_id, expected_cq)` 是对象身份 fence**：仅当 `registry[task_id] is expected_cq` 才删，防迟到的 cleanup 误删同键新 CQ。
- **两把锁**：`_wlock` 全局、极短，只护换引用；`lock_for(task_id)` 返回 per-task `RLock`，是跨服务生命周期事务锁，RunControlService 与 HealthMonitorWorker 共用。可重入是因为 `start_run` 持锁内会再调 `stop_run`。
- **`find_by_source_ip(source_ip)`** 与 `get(task_id)` 是并列的两条查询轴：`get` 锁定某一次 run；`find_by_source_ip` 按点位解析当前 live run（命中多个取 `task_started_at` 最晚者），每次重解析以跟随换 run，不能缓存。调用方：WS `/ai/video?client_id=`、`/api/terminate?client_id=`（兼容入口）；wire 上的 `client_id` 值恒为 `source_ip`。

## CQ = 一次 run，身份构造即定死

一个 CQ 对应一个 `RunIdentity(task_id, step_id, run_id)`，即 `cq.run`，由 RunControlService 在锁内 `runs.allocate` 后注入。`source_ip`、`stage` 同为构造参数；`task_started_at` 在有 run 时取构造时刻，供任务超时看门狗与启动延迟埋点。CQ 上没有 `task_id` / `step_id` 属性，读者一律读 `cq.run.*`。**「未绑定 run」的唯一判据是 `cq.run is None`**（裸建，供纯队列 / 算子单测）。

身份无 setter，热路径免锁直读。切 step 或重启 = 分配新 run、建新 CQ、换槽，不在旧对象上改身份，所以 settlement 天然归属旧 run。

## 状态机 ACTIVE → DRAINING → CLOSED：写入时刻判门

- **ACTIVE**：正常运行。
- **DRAINING**：拆除中（`to_draining()`，幂等），封生产者写，仍放行结算告警与残余 flush。
- **CLOSED**：`close()` 置位并释放 payload（`clear()` 是它的兼容别名）；不可变身份小壳仍可读，供 fence 与日志。

门在**写入时刻**判 state，不在 dispatch 时刻：迟到写落到 DRAINING/CLOSED 的旧 CQ 被拒，不会串到同键新 run。状态读免锁（枚举原子读 + 单调推进）；`_state_lock` 只串行转换本身，不与任何 payload 锁互嵌。两次转换都会静默重置三个 `PressureReporter`。

| 写口 | 门 |
|------|----|
| `append_ca_ready_with_throttle` / `append_ca_raw` / `append_ca_processed` / `set_latest_detection` / `push_detection` / `append_ca_detections` | 非 ACTIVE 拒 |
| `set_latest_rendered` / `set_latest_temporal` | 非 ACTIVE 拒非空写；清空（`None` / `[]`）放行，供拆除期清前端残帧 |
| `append_alarm_record_with_gate` | 仅 CLOSED 拒，DRAINING 放行以保结算告警入账 |
| `take_*_segment` / `drain_*` | 读侧，不设门 |

## 队列与槽位

| 成员 | 锁 | 用途 |
|------|----|------|
| `ca_ready` | 无锁 SPSC deque | 待推理帧；唯一生产者 decoder、唯一消费者 dispatcher |
| `ca_raw` + `latest_raw_frame / latest_raw_timestamp` | `_raw_lock` | raw HLS 落盘缓冲 + 最新原始帧（可视化与健康监控读） |
| `ca_processed` + `_latest_rendered` | `_viz_lock` | processed HLS 落盘缓冲 + WS 推流的最新渲染帧；VizWorker 对同帧连写两者 |
| `_latest_detection` | `_detection_lock` | 最新帧级 `FrameDetection` 快照，供 VisualizationWorker |
| `_latest_temporal` | `_frontend_lock` | 最新时序事件，供前端 overlay 与消息接口 |
| `_slide_window` + `ca_detections` | `_slide_window_lock` | 帧窗（供时序算子）与检测结果落盘缓冲；写回口对同帧连写两者 |
| `_alarm_log`（maxlen 100）+ `_alarm_seq` + `_alarm_gate` | `_alarm_lock` | 告警闸门、seq 与环形日志 |

- **全清顺序**（`_release_payload` 用 `ExitStack` 依次持 6 把锁）：`_raw_lock → _viz_lock → _detection_lock → _frontend_lock → _slide_window_lock → _alarm_lock`。`PressureReporter` 内建锁是叶子锁，`append_*` 先出队列锁再上报。
- **落盘缓冲不触发落盘**：`ca_raw` / `ca_processed` / `ca_detections` 由 recording 的 sweeper 经 `take_*_segment()` / `drain_ca_detections()` 主动拉（[SERVICE_RECORDING.md](SERVICE_RECORDING.md)）。**运行期 drain 者只能是 sweeper 一个线程**，否则入队序竞态会打乱段间 tfdt。
- **`drain_ca_raw / drain_ca_processed(until_ts)`**：`None` 全排空（拆除期）；给值只弹队首 `timestamp <= until_ts` 的连续前缀（断流期，防残段横跨重连 gap）。`drain_ca_detections()` 无栅栏：每帧一行、行间无依赖。
- **`ca_detections` 不能与 `_slide_window` 合并**：帧窗按算子感受野裁剪，拿它当落盘缓冲，某个 stage 调小 `window_seconds` 就会静默丢检测结果。任一源降级的帧不入 `ca_detections`。
- **帧窗保留时长** = `max(10 s, 各算子最大感受野)`，由 InferenceService 经 `set_stream_windows` 配置、只向上扩；`signals_10s` 另按固定 10 s 裁窗。
- **队满丢最旧并计数**：`ca_raw` 计 `frame_drop_total{reason="raw_backpressure"}`，`ca_processed` 计 `hls_backpressure`，`ca_detections` 计 `detection_backpressure`；`ca_ready` 满只计 `frames_dropped_ready`。三条像素队列各挂一个 `PressureReporter`（[DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)）。

## 抽帧：每 N 帧留 1，按帧计数不看墙钟

`append_ca_ready_with_throttle` 先判写门，再推进 `_decimate_counter`：攒满 N 帧放行 1 帧（N = `settings.inference_decimation`，默认 2），长期保留率精确为 1/N；放行的帧遇 `ca_ready` 已满则丢。计数器只由 decoder 线程读写、无锁。

- 输入是 ffmpeg 规范化后的 CFR 流，帧号即等距时间，整数计数天然均匀；墙钟间隔门受解码线程调度抖动、稳定达不到目标率，因此不用。
- 整数因子只命中整除比（30→15/10/7.5…）；非整除需求由模型侧 `model_input_fps` 按 ts 重采样承接。
- `inference_decimation` 是唯一采样旋钮，`settings.inference_fps = raw_fps / N` 是派生 property。HLS 段帧率不用它，由写侧从帧 ts 逐段反推（[DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)）。
- decoder 调本方法之前还有自己的准入背压（`ca_ready` 占用率 ≥ 0.90 即不送，[SERVICE_STREAM.md](SERVICE_STREAM.md)）。

## 容量以秒声明，配置校验只查关系式

四条 CA 队列共用 `ca_maxlen` = `settings.ca_maxlen_seconds`（30）× `raw_fps`（30）= 900；段长 `ca_segment_len` = `ca_segment_seconds`（10）× `raw_fps` = 300。帧数换算只在 `ClientConfig` 属性边界发生，`cq_kwargs()` 是生产建 CQ 的唯一配置出口；`ClientQueues.__init__` 的默认值（900 / 150）只供裸建与测试。取 30 s 是给录制腿留余量：`ca_raw` 丢帧即录像永久空洞，30 s ≈ 3 段，吸收分段消费抖动。

`ClientConfig._validate_config` 只查关系式，不查绝对帧数（其语义会随 `raw_fps` 漂移）：段长 < 5 s；`ca_segment_len > ca_maxlen`（装不下一段，永远触发不了分段）；`ca_maxlen < ca_segment_len × 1.2`（余量不足 20%）。后两条 `if/elif` 互斥。三条都只打 WARNING、不抛异常（「装不下一段」也只是 WARNING，文案带 ❌）。当前余量 3.00×，零告警。

## 告警闸门与前端消息

`append_alarm_record_with_gate(alarm, mode)` 在单个 `_alarm_lock` 内原子完成闸门去重、赋 seq、入环形日志。闸门键 `f"{alarm.metric}:{mode}"`，固定 5 s 冷却；闸门表挂在 CQ 上即一个 run，键里不需要 task_id。被拦的告警不入账。

`/task/message/{task_id}` 经 `client_service.get(task_id)` 取 CQ，`get_alarm_snapshot(since_seq)` 在同一把锁内返回 `(告警增量, max_seq)`，保证 `max_seq ≥ max(a.seq)`、游标不漏告警；另附 `signals_10s`（`get_slide_window_summary()` 按流名聚合，映射到 metric 在 `app/routers/task.py`）。

## 代码来源

- `app/services/client/{__init__,service,instance,queues,config}.py`
- `app/services/run_control/service.py`（CQ 构造与 set / remove）
- `app/types/run.py`（`RunIdentity`）
- `app/routers/task.py`（`/task/message`）、`app/routers/api.py` / `app/routers/ai.py`（`find_by_source_ip`）
- `tests/test_alarm_increment.py`、`tests/test_task_message_api.py`、`tests/test_teardown_identity_fence.py`、`tests/test_client_detections_buffer.py`、`tests/test_client_service_find_by_source_ip.py`
