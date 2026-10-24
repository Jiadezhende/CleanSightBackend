> 更新时间：2026-10-17
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 线程安全与异步解耦设计

实时路径由多线程、多队列和 per-run 状态组成。本文件只写可迁移的原则、判据与反例，本仓库落地只作一行例证，现状细节在对应 SERVICE_ 文件。迟到写入与换代冲突（写门、代次、fence 的闭合判据）专门见 [DESIGN_STALE_WRITES.md](DESIGN_STALE_WRITES.md)。

## 线程安全：先划清共享状态的边界，再决定加什么锁

方法论是**自底向上**：底层组件每个方法各自原子，不依赖调用方持外锁；上层只为「多步组合序列」加锁，不重复保护底层已安全的字段。

### 生命周期事务：一个运行键一把可重入锁，所有发起方共用

同一运行单元的起 / 停 / 重启是跨多个服务的多步事务，**所有能发起它的调用方必须共用同一把锁**；api 一把、后台线程一把，等于没锁。锁要可重入，因为「重启 = 持锁内先停再起」。

- `asyncio.Lock` 管不住独立 `threading.Thread`，所以服务层的线程锁是必要纵深，不是冗余：api 协程经 `asyncio.to_thread` 把持锁段挪出事件循环，服务层 RLock 串行事务，数据层细粒度锁护读写。
- 被编排的下层服务（如推理的 start/stop workflow）不再自持 per-run 锁，互斥由上层事务锁承接。
- 例证：`ClientService.lock_for(task_id)`，`RunControlService` 与 HealthMonitorWorker 共用（[SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)）。

### 换代隔离：写门挡迟到写，身份 fence 挡迟到拆除

- **写门**：运行态对象带单调状态机，在**写入时刻**判状态；门可以不对称，拆除期仍需的收尾写（结算、残余 flush）放行。单调前提下门禁本身即闭合（见 [DESIGN_STALE_WRITES.md](DESIGN_STALE_WRITES.md) §4）。
- **对象身份 fence**：「先决策后拿锁」的发起方在决策时捕获对象引用，拿锁后核对槽位仍是它，否则整段放弃。持锁内决策并执行的发起方没有 ABA，不需要 fence。
- **per-run 不可变**：切换 = 建新对象换槽，不在旧对象上改身份。身份不可变就没有身份锁，也没有「先停旧消费者再切字段」的隐式排序。
- 例证：`ClientQueues` 的 ACTIVE→DRAINING→CLOSED 与 `remove_if` / `stop_run(expected=…)`（[SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)）。

### 锁设计原则

1. **按访问模式分锁，不按资源分锁**：同一业务动作总一起读写的字段归同一把锁（例：`_viz_lock` 同护 `ca_processed` 与 `_latest_rendered`）。
2. **能证明 SPSC 就不加锁**：单写单读、角色固定时，GIL 下 `deque.append/popleft` 原子（例：`ca_ready`）。重启可能出现第二个生产者时，须保证旧生产者先退出（例：`restart_stream` 同步停旧）。
3. **快照避免锁嵌套**：先在轻锁下把要读的字段快照到局部变量，再进重锁，顺带消除 TOCTOU。
4. **多锁固定顺序**：在类 docstring 声明唯一全清顺序，用 `ExitStack` 顺序加锁；叶子锁由调用方在自己的锁外调用。
5. **有不变式的关联值同临界区读**（例：`get_alarm_snapshot` 一把锁内返回 `(增量, max_seq)`，游标不漏告警）。
6. **setter 只赋值**，缓存清理等副作用拆到显式方法。
7. **幂等精确到「完全相同」**，不能只查主键（例：`start_run` 仅 step 与 URL 都不变才幂等，否则全量重建）。
8. **有锁的类在 docstring 维护「锁清单 + 全清顺序」**，`grep` 锁名即可核对（例：`ClientQueues`）。

### 落盘编排：单消费队列管顺序，不跨代复用路径管隔离

写盘的两件事正交，各用一个机制，不要用锁同时解决：

```text
同一目标内的顺序    单消费队列：提交序 == 执行序
跨代的隔离          盘上路径不跨代复用：每代一个目录，写者只写自己那一代
```

- 凡是正确性依赖执行序的写（如位置相关的累计偏移），**加第二个 worker 不报错、只会静默损坏**，所以不要把 worker 数做成配置项。
- 落盘任务带提交时刻的版本句柄；写者不建版本目录，版本被回收后迟到写原子失败，不会重建僵尸目录。
- 跨线程共享的小表免锁的前提：只做单次原子操作（`__setitem__` / `get` / `pop`），不逐元素迭代。
- 例证：`RecordingService` 两条 `SerialTaskQueue` + run 目录（[SERVICE_RECORDING.md](SERVICE_RECORDING.md)）。

## 异步解耦：慢 IO 不直接阻塞实时链路

### 按节奏拆池，经共享快照交接

推理、时序、可视化各按自己的节奏跑，经 per-run 状态对象交接：推理写帧窗、最新检测与落盘缓冲；时序读帧窗、写时序事件与告警；可视化读最新帧与检测、写渲染帧与 processed 缓冲。时序分析或渲染慢不会阻塞 GPU 推理热路径。

### GIL 是多路并发的第一瓶颈：持 GIL 的整帧工作只能挪出主进程

- **判据**：加线程吞吐不涨（1/2/4/8 线程总吞吐持平），旁边多一个忙线程吞吐就塌，说明卡在 GIL 而不是工作本身。这时加线程无效，只有让工作不持 GIL：交给内核（`readinto` 直写数组）、外部进程（ffmpeg 经 stdin 编码），或挪进子进程。
- **共享帧缓冲不复用**：下游各处持有同一帧引用（录像缓冲、推理队列、最新帧快照）时，复用读缓冲会静默覆盖在排的帧；每帧新建并**置只读**，把「误原地写污染全部下游」变成当场 `ValueError`。
- 例证：decoder 每帧 `np.empty` + `readinto`（[SERVICE_STREAM.md](SERVICE_STREAM.md)）、渲染子进程池（[SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)）。

### 跨进程传大块数据：共享内存环，队列只传槽位号

`mp.Queue` 每帧 pickle 并分块写管道，成本全在发送方 GIL 上；TCP 回环更慢。共享内存环只剩一次 memcpy。不出错的前提：

- **槽位生命周期只归生产方**：消费方从不归还，生产方收到答复才复用、判定对端已死时整体 `reset()`。这样不需要跨进程同步空闲表。
- **消费方一律拷出**：不持有共享内存视图，生产方复用槽位改不到已读出的数据；拷贝发生在消费进程，不占生产方 GIL。
- **分配要么全给要么不给**，不够即按背压返回 False，与「在途已满」走同一条路径；环大小 ≥ 在途上限 × 最大批大小，正常时不先于在途上限耗尽。
- **放不进槽位就退回旧路径，不能拒收**：上游对被拒的同一批会无限重试，拒收等于卡死。
- **槽位归还 / 重置与在途记账在同一把锁里**：空闲表 `release` 只追加不查重时，归还落到 `reset()` 之后会产生重复槽位，同一槽位同时分给两批、后写覆盖先写，结果算错对象且不报错。锁顺序单向（代理锁 → 环锁），环不回调代理。
- 例证：`app/services/utils/frame_ring.py`，推理代理与渲染代理共用。

### 子进程池：共享请求队列时故障整组重启

- P 个子进程共享一条请求队列能自然负载均衡，代价是判不出卡住的请求在哪个子进程上，被杀的子进程还可能持着队列读锁——所以故障时 kill 全部、清在途、重建队列再起，不做单个重启。
- 有序性要求（如同一 run 的帧按 ts 进缓冲）用「每键单飞」保证：同一键同时最多一个在途请求。
- 子进程忽略 SIGINT / SIGBREAK：终端 Ctrl-C / Ctrl-Break 广播给整个进程组，子进程先死会被监督线程误判为故障；停机统一由父进程 `terminate` 驱动。
- 完成结果由调用方线程非阻塞拉取（`drain`），代理不设收集线程，下游统计状态仍只有一个线程访问。

### 一条队列一个语义，慢度不同的 IO 不共用

慢 IO 一律经有界队列异步化，上游只付入队成本。慢度差一个量级的两类工作放进同一条队列，快的会被慢的一起背压丢掉，而且静默。

- 队列满即丢任务并 warning，这是背压与容量告警的观察点；**不给无界选项**——无界只是把「丢一段」换成「吃光内存」。
- **大对象队列按在途字节限额，不只按条数**：条数上限 × 单条最大字节才是真实内存上限，单条几百 MB 时 OOM 会先于丢任务发生。入队记账、执行完不论成败归还、入队失败当场归还；条数上限保留作双保险。例证：录制段队列 `queue_max_mb`（[SERVICE_RECORDING.md](SERVICE_RECORDING.md)）。
- **缓冲深度只给需要的那一腿**：推理只要最新帧，积压的陈帧无用，深缓冲留给不许丢帧的录像腿；两腿共用一个上限会让停滞时多占一整份内存。
- **容量规划按「几种停滞不同时发生」取最大，不相加**，并在启动时与可用内存比对、超了拒绝启动（[ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)）。
- 可丢任务用短入队超时；不许丢的任务传大超时并检查返回值、失败时降级同步执行。
- 例证：录制段 / 检测结果 / 告警 / 离线作业各一条队列（`recording`、`recording-detections`、`alarm_queue`、`offline`）。

### 生命周期：内层写者先停，外层队列后停

三种粒度：进程级单例（import 或 lifespan 建）、常驻线程 / 子进程（服务 `start()`→`stop()`）、per-run 动态实例（Actor / decoder，随 run 起停）。

- **关停按嵌套逆序**：产出方在内层先停，承接其产出的队列在外层后停、排空。
- **「外层队列活得更久」只保证已提交的任务不丢**：还在缓冲里、要靠拆除路径显式交出的东西，停机路径必须自己把交出动作走一遍。反例：停机只停推理、不走 per-run 拆除，各 run 缓冲里不足一段的残帧与最后一批检测结果随进程丢失。例证：`run_control.lifespan` 嵌在最内层，停机先逐个 `stop_run`（[SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)）。
- **不可中断的点隔离到子进程**：线程里的 CUDA 同步前向无法被 `stop_event` 打断，放进子进程才能硬收尸（例：`RemoteInferProxy._kill_child` 的 `terminate→join(2)→kill→join(2)`）；主进程侧守护线程都用 `stop_event.wait` 或带超时的 `queue.get`，真可中断。
- **关键副作用不依赖会被硬杀的执行体**：会被杀的一方只产出内存结果，落盘交给活得更久的一方（例：推理写回只进 CQ 缓冲，由 recording 队列落盘）。
- **只读外部进程直接 SIGKILL**：没有产物可损坏、对端能自行回收连接时，优雅退出只是白等（例：`FFmpegDecoder.stop()`）。
- **备查**：模型 / CUDA 上下文在 `stop()` 不显式释放，关进程时由驱动回收；将来若做「不退进程换模型」会变成真泄漏。

## 通用并发件（`app/services/utils/`，不属于任何服务、不 import 兄弟服务）

- **`task_queue.SerialTaskQueue`**：一条有界队列 + 一个消费线程；一次性（`stop()` 后不能再 `start()`）；谁用谁 new，不建全局注册表统一起停（停机顺序属于域）。任务异常在 `_execute` 记 error 吞掉，所以消费线程不包 `guarded_run`；不做重试、优先级、取消、结果回传。
- **`worker_guard.guarded_run`**：包常驻 worker 的整个主循环，崩溃后冷却 2 s 重启、最多连续 3 次，`stop_event` 已置位则不再重启。与函数级重试互补；覆盖不了 segfault 与死锁。用在 `AlarmWorkerPool`、dispatcher、`ClientTemporalActor`、`VisualizationWorkerPool` 四处（容错分层见 [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)）。
- **`pressure.PressureReporter`**：只描述、不决策的 `[PRESSURE]` 周期快照；只接状态型资源，事件照常直接打日志（见 [DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)）。
- **`frame_ring.SharedFrameRing` / `SharedFrameRingView`**：跨进程送帧的共享内存环（上节原则的实现）；同时支持请求-应答式原地写回。

## 代码来源

- `app/routers/api.py`（`asyncio.to_thread` 桥接）
- `app/services/run_control/service.py`、`app/services/client/{service,queues}.py`
- `app/services/inference/online/detection/{dispatcher,infer_proxy}.py`、`app/services/inference/online/visualization/{render_proxy,render_child}.py`
- `app/services/recording/{service,sweep_worker}.py`、`app/services/alarm/{service,alarm_worker}.py`、`app/services/inference/offline/service.py`
- `app/services/utils/{task_queue,worker_guard,pressure,frame_ring}.py`
- `app/services/stream/decoder.py`（SIGKILL `stop()`）、`app/main.py`（lifespan 嵌套）
- `tests/test_api_concurrency.py`、`tests/test_teardown_identity_fence.py`、`tests/test_task_queue.py`
