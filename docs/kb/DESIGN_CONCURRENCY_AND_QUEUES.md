> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 线程安全与异步解耦设计

实时路径由多线程、多队列和 per-run 状态组成。本文件沉淀两类可迁移的原则，本仓库的落地只作例证：

1. 线程安全性：明确哪些状态可共享、由谁读写、用哪把锁保护，避免竞态、错归属和死锁。
2. 异步解耦：把推理、时序、渲染、落盘、外部 IO 拆成独立节奏，避免慢任务卡住实时链路，提高可维护性和故障隔离能力。

## 方向一：线程安全性

重点不是「到处加锁」，而是把共享状态边界划清楚：同一运行单元的生命周期变更串行化；不同用途的数据用不同锁；高频热路径尽量少锁；必须同时清理多个状态时固定加锁顺序。

### 生命周期事务：一个运行键一把锁，所有发起方共用

同一运行单元的起 / 停 / 重启是跨多个服务的多步事务，**所有能发起它的调用方必须共用同一把锁**；各自持锁（api 一把、后台线程一把）等于没锁。锁要可重入，因为「重启 = 持锁内先停再起」。

例证：per-task `RLock` 由 `ClientService.lock_for(task_id)` get-or-create。`RunControlService.start_run` / `stop_run` 全程持它；api 层（`app/routers/api.py`）不自持锁，只经 `asyncio.to_thread` 把同步持锁段桥出事件循环调 `run_control_service`；HealthMonitorWorker 后台线程也走同一把锁。三方共用消除「HM 迟到 cleanup 误删 /start 刚建 CQ」的竞态。`InferenceService` 不自持 per-run 锁：`start_workflow` / `stop_workflow` 的互斥由上层 `lock_for(task_id)` 承接。

覆盖场景：并发启动同一任务幂等返回；改 step/URL 触发停旧全量重建；terminate 与 start 共用同一把锁；不同 task 并发不互相阻塞。

### 跨 run 隔离：状态机写门 + 对象身份 fence

运行单元换代时，旧代的迟到写与迟到拆除是两类不同的风险，各用一道机制挡：

- **写门（挡迟到写）**：运行态对象带单调状态机 `ACTIVE→DRAINING→CLOSED`（转换锁只串行转换本身，不与 payload 锁互嵌）。所有写在**写入时刻**判 state，迟到写落到 DRAINING/CLOSED 的旧对象被拒，不串台到同键新 run。门可以不对称：拆除期仍要放行的收尾写（结算告警、残余 flush）在 DRAINING 放行。例证：`ClientQueues`，详见 [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)。
- **对象身份 fence（挡迟到拆除）**：「先决策后拿锁」的发起方在决策时捕获对象引用，拿锁后核对槽位仍是它，否则整段放弃。例证：`ClientService.remove_if(task_id, expected_cq)`、`RunControlService.stop_run(expected=...)`，防 HM 误删被 /start 抢占重启的新 run。持锁内决策+执行的发起方无 ABA，不需要 fence。

### 锁库存：按访问模式分锁，并写进 docstring

ClientQueues 的锁按职责拆分（身份 `run` / `source_ip` / `stage` / `task_started_at` 为构造定死的不可变值，热路径免锁直读，故**无**身份锁）：

- `_raw_lock`：`ca_raw` 和 latest raw。
- `_viz_lock`：`ca_processed` 和 latest rendered（VizWorker 对同帧连写两者）。
- `_detection_lock`：`_latest_detection`（帧级 `FrameDetection` 原子快照）。
- `_frontend_lock`：latest temporal。
- `_slide_window_lock`：帧级 `FrameDetection` 滑窗 + `ca_detections` 落盘缓冲（写回口对同一帧连写两者）。
- `_alarm_lock`：alarm log、seq、gate。
- `PressureReporter` 内建锁：叶子锁，只护其几个标量；`append_*` 一律先出队列锁再上报，不与上面任何锁互嵌。

clear 时固定顺序（6 把 payload 锁）：

```text
_raw_lock -> _viz_lock -> _detection_lock
-> _frontend_lock -> _slide_window_lock -> _alarm_lock
```

### SPSC 队列

单写单读且角色固定时不需要锁。例证：`ca_ready` 是无锁 deque——单生产者 decoder、单消费者 dispatcher，依赖 CPython GIL 下 `deque.append/popleft` 的原子性。其他共享队列使用明确锁保护。

### 落盘编排：零锁，靠单消费队列 + 路径不跨代复用

写盘编排的两件事是正交的，各用一个机制构造，不要用锁去同时解决：

```text
同一目标内的顺序    由单消费队列的提交序构造（提交序 == 执行序）
跨代的隔离          由盘上路径不跨代复用构造（每代一个目录，写者只写自己那一代）
```

- **顺序**：「一条队列 + 一个消费线程」让提交序即执行序。凡是正确性依赖执行序的写（如位置相关的累计偏移），**加第二个 worker 不报错，只会静默损坏**，所以不要把 worker 数做成配置项。
- **隔离**：落盘任务带提交那一刻的版本句柄，写进该版本的目录；旧一代的迟到写不影响新一代。写者不建版本目录，版本被回收后迟到写原子失败、不会重建僵尸目录。这比「写前与注册表比对、不是当前代就丢弃」少一个比对点，也不需要换代时删旧产物（原则见 [DESIGN_STALE_WRITES.md](DESIGN_STALE_WRITES.md)）。
- **跨线程共享的小表**免锁的前提是只做单次原子操作（`__setitem__` / `get` / `pop`），不逐元素迭代。

例证：`RecordingService` 两条 `SerialTaskQueue`（`app/services/utils/task_queue.py`）各自单消费线程，同一 run 内相邻段 tfdt 单调建立在单消费者上，`config/recording_config.yaml` 因此没有 `workers` 项；任务只带 `RunIdentity`，写进该 run 目录，run 目录只由 `runs.allocate` 建；`_pending_flush` 三线程免锁。完整推导与失效表现见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)；`app/storage` 各域自身不持锁，串行由调用侧构造。

### 生命周期粒度与关停

**三种生命周期粒度**，关停按嵌套逆序（例证：`app/main.py` lifespan `health_monitor → stream → (cleanup, alarm) → recording → inference`，inference 最内、最先停）：

| 粒度 | 例证 | 创建 → 销毁 |
|------|------|-------------|
| 进程级单例 | `client_service` / `stream_service` / `alarm_service` / `recording_service` / `inference_service` / `offline_job_service` / `run_control_service` / `cleanup_worker` / `health_monitor_worker` | import·lifespan → lifespan 关闭 |
| 常驻线程/进程 | dispatcher / 推理子进程（`RemoteInferProxy` spawn）/ viz worker 池 / 录制两条队列线程 + 录制 sweeper / 告警池 / cleanup 线程 / 离线作业队列线程 | service `start()` → `stop()` |
| per-run 动态实例 | TemporalActor / FFmpegDecoder | `start_workflow`·`start_stream` → `stop_workflow`·`stop_stream` |

原则：

- **把不可中断的点隔离到子进程**：线程里的 CUDA 同步前向无法被 `stop_event` 打断，放进子进程后可以硬收尸。例证：推理子进程内 `StageWorker` 的 GPU 前向；`RemoteInferProxy.stop()` → `_kill_child()` 用 `terminate→join(2.0)→kill→join(2.0)`，半途的前向随进程被杀、不残留孤儿；主进程侧 collector / supervisor / dispatcher 等守护线程都真可中断（`stop_event.wait(interval)` 或带超时 `queue.get`）。
- **关键副作用不依赖被 join 的 worker**：会被硬杀的执行体只产出内存结果，落盘交给活得更久的一方。例证：推理写回口只把检测结果放进 cq 缓冲，拆除期由 `RunControlService.stop_run` 在控制线程调 `recording_service.flush_residual(cq)` 交给 recording 队列，推理子进程被硬杀不影响已写回的结果；进程直接停机（不经 `stop_run`）时 cq 里最后不到 1 s 的检测结果与不足一段的残帧可能没人拉，已接受。
- **只读外部进程直接 SIGKILL**：没有产物可损坏、对端能自行回收连接时，优雅退出只是白等。例证：`FFmpegDecoder.stop()` 用 `kill→wait(reap)`，ffmpeg 只解码到 `pipe:1`、RTSP 对端是自有 mediamtx_gateway；卡读时优雅路径白耗 ~2s 后照样 SIGKILL，直接 kill ~ms。
- **备查**：模型 / CUDA 上下文在 `stop()` 不显式释放——关进程时驱动回收无碍；若将来做「不退进程的重启/换模型」会变真泄漏。

## 方向二：异步解耦与防卡死

让每类工作按自己的节奏运行。实时链路只传递必要快照或入队任务，慢推理、慢渲染、慢磁盘、慢 HTTP 不直接阻塞上游，从而降低「一个慢点拖死整条链路」的风险。

### 三池解耦

推理、时序、可视化通过 ClientQueues 解耦：

- 推理写 slide_window、latest_detection 和 ca_detections。
- 时序读 slide_window，写 latest_temporal 和 alarm。
- 可视化读 latest_detection / latest_frame / latest_temporal，写 ca_processed / latest_rendered。

这种设计避免时序分析或渲染阻塞 GPU 推理热路径。

### 落盘 / 上报的队列解耦：慢度不同的 IO 不共用一条队列

慢 IO 全部经有界队列异步化，上游只承担入队成本。**一条队列一个语义**：慢度差一个量级的两类工作放进同一条队列，快的会被慢的一起背压丢掉，而且丢得静默。

例证（各自独立起停）：

- **录制段**：`RecordingService` 的 `"recording"` 队列（`queue_size: 100`，恒 1 个消费线程），隔离视频段写盘、ffmpeg fMP4 转码、playlist 追加。不能扩 worker——顺序即正确性（见上）。
- **检测结果**：`RecordingService` 的 `"recording-detections"` 队列，与段写分开：段写单段 0.26–3 s，检测结果排在它后面会跟着被背压丢。
- **告警**：`AlarmService` 的 `alarm_queue` + `AlarmWorkerPool`（1 worker）隔离外部 HTTP 上报。
- **离线作业**：`OfflineJobService` 的 `SerialTaskQueue("offline", maxsize=20)`，一次一个作业；耗时计算放子进程（CPU 隔离 + 降优先级 + 可 kill），队列线程只起停与监视；队满即 409。

共同约定：

- 队列满即丢任务并 warning（录制侧丢一段 ≈ 丢 10 秒录像），是背压与容量告警的观察点；**不给无界选项**——无界只是把「丢一段」换成「吃光内存」。
- 关停顺序由 lifespan 嵌套保证：recording / alarm 都在 inference 外层，`inference.stop()` 期间结算告警仍能入告警队列、sweeper 仍在拉，之后队列才排空退出。

### 通用件：队列、线程自愈、压力快照

三件跨服务复用的并发工具在 `app/services/utils/`（不属于任何服务、不许 import 兄弟服务）：

- **`task_queue.SerialTaskQueue`**：一条有界队列 + 一个消费线程；一次性（`stop()` 后不能再 `start()`，要重来就新建）；一条队列一个语义、谁用谁 new，不建全局注册表统一起停（停机顺序约束属于域）。任务异常在 `_execute` 里记 error 吞掉——一个任务炸掉不能带走整条队列，故消费线程**不包** `guarded_run`；不做重试、优先级、取消、结果回传。
- **`worker_guard.guarded_run`**：包裹常驻 worker 的**整个主循环**，崩溃后冷却重启（`max_restarts=3`、`cooldown=2.0`），`stop_event` 已置位则不再重启。与函数级重试（如告警上报 `_report_with_retry`）互补；覆盖不了 segfault 与死锁。例证：`AlarmWorkerPool`、dispatcher、`ClientTemporalActor`、`VisualizationWorkerPool` 四处。容错分层见 [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)。
- **`pressure.PressureReporter`**：**只描述、不决策**的 `[PRESSURE]` 周期快照日志（默认每 10s 至多一条、平稳时静默，专用 logger `app.pressure`）；压力 = 调用方谓词 OR 任一 `*_total` 自上次报告后增长。内建锁是叶子锁，调用方须在自己的锁外调用。只有状态型资源接它（CQ 三条 CA 像素队列、dispatcher 的 stage deque）；事件（子进程死亡、落盘失败）照常直接打日志。详见 [DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)。

### 可维护性收益

解耦后，每个模块的职责更窄：

- Stream 只关心拉流和产帧。
- Inference 只关心推理结果和时序告警，写回口零 IO。
- Visualization 只关心最新快照的渲染。
- Recording 只关心 HLS 段与检测结果何时拉、按什么顺序写。
- Alarm 只关心告警上报；TTL 回收在 `app/daemons/cleanup/`。
- HealthMonitor 只关心失联、重连和统一清理。

性能问题和故障边界因此更容易定位，新增检测点、调整落盘策略、替换外部告警接口时不必重写实时主链路。

## 锁设计原则（可复用方法论）

基础方法论：**自底向上构建线程安全**——底层组件（`ClientQueues`）每个方法各自原子、不依赖调用方持外锁；上层只为「多步组合序列」加锁，不重复保护底层已安全的字段。核心思路：**按访问模式分锁，不按资源分锁**——同一业务动作总一起读写的字段归同一把锁（如 `_viz_lock` 合并 `ca_processed`+`_latest_rendered`，VizWorker 一次加锁写两者）。

1. **识别 SPSC，消除不必要的锁**：单写单读且角色固定时，GIL 已保证 `deque.append/popleft` 原子（`ca_ready`：decoder 唯一写、dispatcher 唯一读，无锁）。能证明不需要锁就不加。
2. **快照模式避免锁嵌套**：热路径读多把锁保护的字段时，先在轻锁下快照到局部变量再进重锁，两锁生命周期不重叠——同时消除 TOCTOU。
3. **固定全清顺序防死锁**：同时持多锁（`clear()`）时死锁充要条件是不同路径乱序取锁；在类 docstring 声明唯一顺序，用 `contextlib.ExitStack` 顺序加锁、逆序释放。
4. **关联值同临界区读**：存在不变式的两值必须一次加锁同读。例：`get_alarm_snapshot` 单 `_alarm_lock` 内返回 `(增量告警, max_seq)`，保证 `max_seq ≥ max(a.seq)`，游标不漏告警。
5. **赋值与副作用分离**：setter 只赋值，缓存清理/事件触发等副作用拆到显式方法（合约写 docstring）。更进一步可让运行态对象**per-run 不可变**：身份构造定死、无 setter 副作用，清理走 `close()` / `_release_payload`。
6. **状态机转换先退旧再进新**：生命周期切换严格「旧状态完整退出→再建新」。例：`RunControlService` 先 `to_draining`→停 decoder/actor→分配新 run、建新 CQ；per-run 不可变让 settlement 归属天然正确，无需「先停旧 actor 再切字段」的隐式排序。
7. **按业务层级纵深分锁**：不同调用来源需各自锁层——api 协程经 `asyncio.to_thread` 桥出、服务层 `lock_for(task_id)` RLock 串行事务、数据层细粒度锁护读写。关键：`asyncio.Lock` 管不住独立 `threading.Thread`（HealthMonitorWorker），故服务层 RLock 是必要纵深，非冗余。
8. **幂等语义精确到「完全相同」**：不能只查主键。例：`RunControlService.start_run` 仅当 `step_id` 与流 URL 均不变才幂等返回，否则全量停旧重建——低频生命周期操作，全量重建的简单性优于部分更新的边界复杂度。

每个有锁的类应在 docstring 维护「锁清单 + 全清顺序」（`grep` 锁名即可验证代码与文档一致），见 `ClientQueues` docstring。

## 代码来源

- `app/routers/api.py`
- `app/services/run_control/service.py`
- `app/services/client/service.py`（`lock_for` / COW / `remove_if`）
- `app/services/client/queues.py`（`RunState` 状态机 + 锁库存 + `ca_detections`）
- `app/services/inference/online/service.py`
- `app/services/inference/online/detection/{dispatcher,infer_proxy,service}.py`
- `app/services/inference/online/temporal/actor.py`
- `app/services/inference/online/visualization/visualization_worker.py`
- `app/services/inference/offline/service.py`（离线作业队列）
- `app/services/alarm/{service,alarm_worker}.py`
- `app/services/recording/{service,sweep_worker}.py`（录制零锁模型、两条队列）
- `app/services/utils/{task_queue,worker_guard,pressure}.py`
- `app/storage/runs.py`（run 目录分配）
- `app/main.py`（lifespan 关停编排）
- `app/services/stream/decoder.py`（SIGKILL `stop()`）
- `tests/test_api_concurrency.py`
- `tests/test_teardown_identity_fence.py`
