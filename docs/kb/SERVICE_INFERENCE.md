> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Inference Service

推理服务负责 stage 路由、模型推理（L1）、时序分析 + 判定（L3/L4）、可视化与结算告警（**在线段**），以及 run 结束后的全序列动作分割（**离线段**）。检测结果与离线产物的落盘格式归 `app.storage.inference`，在线检测结果的落盘编排归 recording（见「推理产物落盘」）。

包按链路分两段 + 一层共享（`app/services/inference/__init__.py` docstring）：

```text
app/services/inference/
  __init__.py       lifespan()：inference_service 先起后停，offline_job_service 后起先停
  config.py / stage_factory.py / resample.py     online / offline 共享层
  online/           service(InferenceService) / instance(inference_service) / naming / types / render
                    + detection/ temporal/（契约包，各带 impl/）+ visualization/（活体包）
  offline/          segmenter / runner / cli / service(OfflineJobService) / instance(offline_job_service) + impl/
```

- **online 与 offline 运行期互不 import**，共用的只能放顶层共享层。`offline/cli.py` 不得被包内任何模块 import；`offline/service.py` 也不 import runner / torch，CLI 只以子进程命令出现。
- 契约包（`online/detection`、`online/temporal`、`offline`）顶层放基类 + 框架，业务实现收在各自 `impl/`；同一检测点三段实现放各 `impl/` 下的**同名文件**，业务聚合由 YAML stage 绑定表达。
- 包 `__init__` 零 re-export（只有 `lifespan()`），消费方走深路径；两个单例各在 leaf 模块 `online/instance.py` / `offline/instance.py`。
- 测试替身 `MockDetector` / `BrushRulesSegmenter` 在 `tests/doubles.py`（不在生产包内），由测试注入的 config 引用。

## 包结构（子包 = 分层）

| 子包/文件 | 层 | 关键类 | 职责 |
|-----------|----|--------|------|
| `config.py` | 共享 | `InferenceConfig` / `StageConfig` | 加载 YAML；`require_offline(step_id)` 是离线可跑的唯一校验 |
| `stage_factory.py` | 共享 | `StageFactory` | 按 stage 实例化 Detector / Operator specs / OfflineSegmenter；建 metric 与别名表 |
| `resample.py` | 共享 | `resample_by_ts` | 按帧 ts 降采样到模型契约帧率（在线 Operator 与离线 Segmenter 共用） |
| `online/service.py` | 编排 | `InferenceService` | 在线生命周期、`resolve_stage`、per-run workflow（见下节） |
| `online/types.py` | L1 | `DetectionTask` | online 内部唯一传输对象（入参）；出参直接是 `FrameDetection` |
| `online/render.py` | Viz | `RenderSpec` / `RenderItem` / `RenderType` | Detector → 可视化的渲染描述 |
| `online/detection/detector.py` | L1 | `Detector`（含 `YOLODetector`） | 无状态 GPU 推理抽象，帧→`DetectorOutput` + 可视化数据；多 run 共享 |
| `online/detection/dispatcher.py` | L1 | `StageAwareDispatcher` | **唯一提交者**：`_fetch_and_dispatch_round` pop 各 run `pop_ca_ready()`、**捕获 CQ 句柄**进 `_stage_queues[stage]` deque，再 `_drain_and_submit` peek-commit 轮转排空 → `proxy.submit` |
| `online/detection/infer_proxy.py` | L1 | `RemoteInferProxy` | 主进程侧代理：spawn 推理子进程、`submit`/pending/collector/supervisor；GPU 前向不在主进程 |
| `online/detection/stage_worker.py` | L1 | `StageWorker` + `run_stages`（子进程 entrypoint） | 子进程内 `{stage: StageWorker}` 固定 map，按 stage 路由做串行前向 |
| `online/detection/service.py` | L1 | `DetectionService` | 装配 proxy（先构造）+ dispatcher；`_write_back_results` 单入口写回（作 `write_back=` 注入给 proxy collector 回调） |
| `online/temporal/operator.py` | L3/L4 | `Operator` / `TemporalOperator` | analyze+judge 合并，持 `_sm`，`subscribes` 显式，`window_seconds` 感受野；`TemporalOperator` 另持 CPU 上的 torch 时序模型做动作识别 |
| `online/temporal/actor.py` | L3/L4 | `ClientTemporalActor` | per-run 2Hz tick，跑 operators，烧 stage 别名，收结算告警 |
| `online/temporal/alarm_sink.py` | L4 | `persist_alarms()` | 过闸（CQ gate）+ 经 `alarm_service` 落库，实时/结算统一入口 |
| `online/visualization/visualization_worker.py` | Viz | `VisualizationWorker` / `VisualizationWorkerPool` | 读快照渲染，写 `ca_processed` + `_latest_rendered`；池由 `InferenceService.start()/stop()` 起停 |
| `online/visualization/visualizer.py` | Viz | `FixedVisualizer` | 按 `RenderSpec` 固定渲染 |
| `online/detection/impl/` | 接入点 | bubble/bending/clean | 具体 Detector 子类（一文件一基类） |
| `online/temporal/impl/` | 接入点 | bubble/bending/clean | 具体 Operator 子类（与检测器同名文件） |
| `offline/segmenter.py` | 离线 | `OfflineSegmenter` | 离线策略基类：`preprocess` / `segment` / 可选 `label_probs()` |
| `offline/runner.py` | 离线 | `OfflineRunner` / `OfflineRunSpec` / `OfflineRunResult` | 一次运行的编排：锁定 run → `read_detections` → 策略 → 校验 → 写 `label_probs.npz` + `temporal.jsonl` |
| `offline/cli.py` | 离线 | `main`（`run` / `query`） | `python -m` 入口：CPU 隔离、末行 JSON |
| `offline/service.py` | 离线 | `OfflineJobService` / `OfflineJob` | 作业服务：`SerialTaskQueue` 串行、每作业起 CLI 子进程 |
| `offline/impl/` | 接入点 | clean | CLEAN 离线策略（默认 `CleanNodepGRUSegmenter` + 三个整段双向模型） |

## InferenceService（在线生命周期编排）

`online/service.py`，单例 `inference_service`（`online/instance.py` 模块级构造）。**构造零副作用**（不读 settings、不加载 stage 配置、不建 worker 池），重组件与 stage 配置在 `start()` 的 `_build_components` 里建——否则 import 本包就会经 `stage_factory` 的 importlib 拉起全部 impl 与 torch。公开方法：

- `start()` / `stop()`：建并起 `DetectionService`（proxy + dispatcher）与 `VisualizationWorkerPool`，初始化 naming 表；`stop()` 两阶段（先 `signal_stop` 全 actor，再逐个 join 收 settlement，经 `alarm_sink.persist_alarms(mode=SETTLEMENT)` 落库——进程停机路径的兜底）。不做任何检测结果 flush。
- `start_workflow(cq)`：**不碰存储**（每次 run 由 run_control 分配自己的 run 目录），只按 `cq.stage` 实例化 operators、按订阅流最大 `window_seconds` 调 `cq.set_stream_windows`、建并起 `ClientTemporalActor`；stage 无 operator spec（`rules: []`）则不建 actor。入参是 RunControlService **已注册**（`client_service.set`）的 CQ——本方法不碰注册表（set/remove 均归 RunControlService，与 `stop_run` 对称）。
- `stop_workflow(cq) → List[Alarm]`：pop actor、`finalize_and_stop` 收结算告警并返回（交 RunControlService 经 `alarm_sink` 落库）。**不收尾检测结果**：cq 落盘缓冲里剩下的由 RunControlService 紧接着调 `recording.flush_residual(cq)` 交出（在本方法之后、`cq.close()` 之前）。
- `resolve_stage(step_id) → str`：**恒等路由，无兜底 stage**。`str(step_id)` 在生效集合（有 detector 的 stage）→ 原样返回；YAML 未定义 → `ValidationError`（「未在推理配置中定义」）；定义了但无 detector → `ValidationError`（「未配置在线检测」）。两者都是 400，由 RunControlService 在锁外、动旧 run 之前调用。入参按 int 传入，故 `"02"` 与 `2` 对到同一 stage。stage 是 CQ 不可变身份的一部分（构造时定死）。
- `status()`。

**构造失败即启动失败**：`_get_stage_configs` 逐 stage 调 `StageFactory.create_detectors_for_stage` / `create_operators_for_stage`，任一 detector/operator 导入或构造失败、rule 缺 `class` / `subscribes` 即抛，整体包成 `RuntimeError` 冒到 lifespan，后端起不来；一个生效 stage 都没有同样失败。YAML 里没配 detector 的 stage 只是「不生效」（记日志），不报错。运行时推理失败走逐帧降级（见「L1 写回」），从不切 stage。

`start_workflow` / `stop_workflow` 的互斥由 RunControlService 的 `lock_for(task_id)` 承接，本类不自持 per-client 锁；`_actors: {task_id → ClientTemporalActor}`。

## L1 推理进程隔离与单提交者管线

GPU 前向拆在独立 **spawn 子进程**（`stage_worker.run_stages`），主进程无 CUDA context、不做 GPU 前向。根因：单路 clean 卡 ~10fps 天花板经隔离实验定案为 **GIL 争用/线程调度**（GPU 时钟满但 SM ~12%，kernel 发射线程被 viz/temporal/HLS/dispatcher 抢 GIL 饿到），非 compute-bound；隔离后推理循环独占一把 GIL，稳定到名义 ~15fps。

- **主进程无 CUDA context**：实时时序 GRU（temporal）仍 import torch 但钉 `torch.device("cpu")` + 惰性加载；主进程保留的 detector 实例永不加载模型，viz 只调 CPU 侧 `prepare_visualization_data`。GPU/CUDA context 仅在子进程。**必须 spawn**（CUDA+fork 不安全），子进程从 YAML 自建 `{stage: StageWorker}` map、`.pt` 惰性加载、按 stage 路由串行前向。
- **单提交者 + peek-commit 轮转排空**：dispatcher 是唯一提交者。每轮先 `_fetch_and_dispatch_round`（pop 各 run `pop_ca_ready`、捕获 CQ 进 `_stage_queues[stage]` deque），再 `_drain_and_submit`：外层 while 转圈、每圈每 stage `_peek_batch`（切片看不移除）→ `submit`，接了才 `_commit_pop`（popleft），被拒即 return（帧原封留 deque、背压沿链上传），整圈无进展即停——显式「每 stage 每圈一批」公平分配，防靠前 stage 吃满饿死后面。
- **dispatcher↔proxy 接口**只 `submit_batch=proxy.submit` 一条（布尔背压）：请求限流是 proxy 固有职责（`inflight >= max_inflight` 即返 False），不外泄计数。背压是沿链上传的传播：`submit` False → 帧留 deque → `infer_backlog` 淘汰 → 上游。
- **异步管线**：`submit` 起 req_id、`pending[req_id]` 存轻量记录 `_Pending(cq, timestamp, frame_width, frame_height)`（分辨率从原始帧 `shape` 盖章，是帧分辨率的唯一采集点）后弃 frame 引用、`inflight++`，子进程前向与主进程下一批 dispatch 重叠。**`cq` 句柄不过进程边界**——切口在纯数据 `StageWorker._infer_models(frames, timestamps)`，cq 只在 dispatcher 捕获、collector 用。collector 守护线程据 req_id 从 `pending.pop` 组装 `FrameDetection(ts, by_source, frame_width, frame_height, cq)` 回调写回。单子进程单 req_q FIFO，同 stage 保序；埋点上移主进程。
- **背压反馈接缝（未接通）**：`_fetch_and_dispatch_round` 有透明 `_admit_to_stage`（恒 True）+ 预留 `_stage_backpressure`（drain 侧写、admit 侧读的单向跨轮通道），供未来「入口按 ts 降帧」，待观测到 inflight 长期贴 max 再接通。

## RemoteInferProxy 防泄漏与监督判据

collector + supervisor 两守护线程护住子进程边界，防孤儿 pending 与静默不可用：

- **防泄漏**：pending 有界（`max_inflight=8`，满则拒收、帧留 deque 由 dispatcher 按 `infer_backlog` 淘汰）；collector 每条响应 `pending.pop(req_id)`；子进程死 / CUDA wedge → supervisor 清空 pending、`inflight` 归零、退避重 spawn。迟到 / 跨 run 结果走原写回口，由 `cq.is_active()` 迟到门挡住；落盘侧的跨代隔离由 run 目录承担（迟到批只可能进它自己那代的 cq 缓冲）。
- **监督三判据**（`_supervise_loop` 并列）：`dead`（进程死）、`wedged`（inflight>0 且久无响应）、`_check_not_ready`（**活着但没就绪**）。第三条补一个原本不可恢复的静默失效：`_spawn_child` 只 `ready_ev.wait(ready_timeout=120s)` 一次，warmup 超时（冷盘加载大权重 / GPU 被占 / 驱动 hang）则 dead/wedged 同时哑火 → 永不重启、全链 0 推理。`_check_not_ready` 两步：① 补收迟到就绪信号（读同一 `ready_ev` 置 `_child_ready`，不重来省一次加载）；② 仍超 `ready_timeout` → 判失败走 `_handle_child_failure` kill + 清 pending + 退避重 spawn。模型文件补回 / GPU 让出后自动恢复。
- `frame_drop_total` 在推理链上的 reason：dispatcher 侧 `infer_backlog`（deque 满淘汰）、proxy 侧 `infer_child_down`（stop 时未及排空）、`infer_child_restart`（重启清孤儿）、写回口 `stale_run`（迟到结果）。

## 进程边界收尸契约（daemon 线程 ≠ 进程能退）

主进程侧的线程/进程启动关系：

```text
后端主进程（uvicorn，非 daemon 主线程）
├── DetectionService.start() → RemoteInferProxy.start()
│   ├── _spawn_child(): req_q / resp_q = ctx.Queue()；ctx.Process(target=run_stages, name="InferChild", daemon=True)
│   ├── Thread("InferCollector", daemon) → resp_q.get → write_back
│   └── Thread("InferSupervisor", daemon) → 1s tick 看门狗
└── QueueFeederThread(req_q)  ← 主进程侧，Queue 在首次 put 时惰性拉起，daemon
```

**`QueueFeederThread` 是 daemon，但 `multiprocessing` 的 `atexit` 钩子会无超时 `join()` 它**——于是它卡住就挂死整个后端进程的退出：uvicorn 已打完 `Application shutdown complete` / `Finished server process`，进程仍不退；再按一次 Ctrl-C 拿到 `atexit → multiprocessing/queues.py _finalize_join → thread.join()` 的 KeyboardInterrupt 栈。触发条件是子进程 wedge（`子进程失败 dead=False wedged=True`）：它不读 `req_q`，管道 64KB 灌满而一批帧是 MB 级，feeder 阻塞在 `send_bytes`。

**此时 `q.close()` 一个都救不了**：它只往内存 buffer 追个哨兵（阻塞中的 feeder 走不到那步），且**不关**本进程持有的读端 fd，写操作连 EPIPE 都拿不到，永久阻塞。

故 `_kill_child` 的队列收尸是**三步**，缺一步就留尸体（`_spawn_child` 另需在建 `req_q` 后、首次 put 前预置 `_ignore_epipe = True`，否则每次收尸都往 stderr 打一坨 `BrokenPipeError`）：

1. `req_q._reader.close()` —— 断本进程这侧读端，阻塞的写立刻 EPIPE、feeder 自退并释放它压着的整批帧内存。**只对 `req_q` 做**，`resp_q` 的读端正由 collector 使用。（= CPython 自己的 `Queue._terminate_broken`。）
2. `cancel_join_thread()` —— 两个队列都做：注销 atexit 里那个无超时的 `thread.join` 终结器。它以线程为宿主，队列被 `_spawn_child` 换掉后仍留在 `_finalizer_registry` 里；子进程真杀不死（CUDA D 态）时读端还开着、EPIPE 不来，留一具尸体就够让进程退不出去。
3. `close()`。

收尸只在关停/重启路径（`stop()` 与 `_handle_child_failure` 的 kill→退避→重 spawn），在线推理链路、进程边界数据格式、背压语义均不受影响。

## 检测契约与时序契约（`app/types/`）

在线与离线共用同一套跨服务契约，按产出层命名；落盘编解码在 `app/storage/inference/` 同名配对（`_detection.py` / `_temporal.py`，见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)）。

```text
层   契约模块                    类型（粒度由细到粗）                         落盘
L1   app/types/detection.py      DetBox → DetectorOutput → FrameDetection     detections.jsonl
L3   app/types/temporal.py       TemporalEvent / TemporalSegment；LabelProbs   temporal.jsonl；label_probs.npz
```

- **检测三型，一粒度一个名词**：`DetBox`（一个框：`bbox/confidence/class_id/class_name/extra`；`extra` 是单框派生量扩展口，不落盘，当前无人使用）→ `DetectorOutput`（一个检测器×一帧：`boxes/metadata/timestamp/success/error`，失败时 `success=False`、`boxes=[]`）→ `FrameDetection`（所有检测器×一帧：`ts/by_source{流名: DetectorOutput}/frame_width/frame_height/cq`）。`detection.py` 纯 stdlib。
- **`FrameDetection` 是唯一的帧级检测对象**：collector 组装、写回口分发、落盘与离线回读都是它。`cq` 字段（`repr=False, compare=False`，标 `Any` 因 types 不得依赖 services）只在 collector → 写回口这一段有值，写回口取走后置 None，留存下来的帧（帧窗 / 快照 / 落盘缓冲 / 离线回读）一律不带。
- **时序两型**：`TemporalEvent(producer, signal, value, ts, conf=1.0, meta={})`（点，`ts` 必填无默认）、`TemporalSegment(producer, label, start, end, conf=1.0, meta={})`（闭区间，单帧段 `start == end`）。三条硬约束（模块 docstring）：① `ts/start/end` 是**帧捕获墙钟 ts（epoch 浮点秒）**，与 `FrameDetection.ts`、HLS `.idx` 同源同值——这是全仓「时间一律 int 毫秒」的具名例外，对外出口（`/ai/temporal`、`/lab-f3m8/label-probs`）换算成媒体毫秒；② `producer` 是产出者身份唯一真源；③ `meta` 只放伴随观测量，不放被代码读来做判断的键。身份键由落盘路径（`RunIdentity`）携带，`type` 判别字段归 storage codec。
- **`LabelProbs`**（`frozen=True, eq=False`）：`ts [T] float64`（与 `detections.jsonl` 位级相等）、`probs [T,C]`、`labels`（含背景类）。它是**可视化旁路，不是事实**，不参与任何判断；形状一致性由产出侧（Runner）校验。`temporal.py` 因它引入 numpy。
- `TemporalEvent` 当前**零生产者**：在线 Operator 的状态共享在 `_sm`，不产事实对象；存储层也因此没有 `append_temporal`。

## L1 写回：单入口 + 句柄化 + 状态门 + 零 IO

`DetectionService._write_back_results(List[FrameDetection])` 是 L1 唯一写回口，作 `write_back=` 注入给 proxy、由 collector 守护线程回调（proxy 只「组装 + 回调」，不知 cq 状态 / stale_run）。逐帧：

1. `cq, frame.cq = frame.cq, None`——取走句柄并清空（留存的帧不带 cq，不成 cq → 帧窗 → 帧 → cq 的引用环）。
2. `cq.is_active()` 为假（DRAINING/CLOSED）→ 整帧丢弃，`frame_drop_total{reason="stale_run"}`。
3. ACTIVE 则同一对象三处投递（不复制）：
   - `cq.push_detection(frame)` → 帧级 `_slide_window`（`Deque[FrameDetection]`，供 L3；异步缓冲解速差 ~15fps ↔ 2Hz）；
   - `cq.set_latest_detection(frame)` → 原子快照（供 Viz；Viz 的 stage 取自 `cq.stage`）；
   - `cq.append_ca_detections(frame)` → 落盘缓冲 `ca_detections`。**降级帧不入落盘缓冲**：任一源 `success=False` 的帧照写帧窗与快照（画面照常、只是没框，逐源打 warning），但不落盘——落盘格式不带 `success`，空框会被离线当成「没检出」；失败时段在离线侧是时间空洞，整个 run 全失败则离线 `skipped`。

**本方法不碰盘**（写回线程零 IO），全程携 CQ 句柄（dispatcher pop 时捕获），**无 `client_id` 反查**。三个写入口自身也各自内建 ACTIVE 门，顶层这道只是提前退出 + 计数。

## 推理产物落盘（不在本服务）

`InferenceService` 不持 store、不管 flush；三份产物全落在 run 目录 `{root}/{task_id}/{step_id}/{run_id}/inference/`，格式与读写口归 `app.storage.inference`：

| 产物 | 写者 | 路线 |
|------|------|------|
| `detections.jsonl`（`FrameDetection`，精简投影） | recording 的 sweeper 每 tick `drain_ca_detections` → 第二条 `SerialTaskQueue`（`recording-detections`）→ `append_detections(cq.run, frames)`；拆除时 `flush_residual(cq)` 全排空 | 追加 |
| `temporal.jsonl`（`TemporalEvent \| TemporalSegment`） | 离线 `OfflineRunner._replace_segments`（唯一写者） | 整体原子替换，调用方 read-merge-write |
| `label_probs.npz`（`LabelProbs`） | 离线 `OfflineRunner._maybe_write_label_probs` | 整体原子替换 |

- **拉模式**：写回口只碰 cq，由 recording 来拉，不产生 inference → recording 依赖边。细节见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)（第二条队列、失败不重试）与 [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)（`ca_detections` 缓冲）。
- 停机：`app/main.py` 把 `recording.lifespan()` 包在 `inference.lifespan()` 外层，队列比写者活得久；进程直接停机（非 `stop_run`）时 cq 里 ≤1s 的检测结果可能没人拉，已接受。

## Detector / Operator 两粒度框架

- **Detector**（流源，分组粒度，无状态共享）：`name`（= 产出流名 = `by_source` key）、`infer_batch(frames, timestamps) → List[DetectorOutput]`（**唯一推理入口**，无单帧 `infer()`）、`prepare_visualization_data(output) → RenderSpec`。`timestamps` 是帧捕获真值锚点（源自 `Frame.timestamp`），须原样写入对应 `DetectorOutput.timestamp`，令其与 `FrameDetection.ts` 同值——collector 按 req_id 把同帧各流装进一个 `FrameDetection.by_source`（供 L3），detector 不得自造时间戳。YOLO 类继承 `YOLODetector` 复用惰性加载/batch/CUDA 异常转换，`_adapt_output` 产 `DetectorOutput(boxes, metadata={"model": "yolo"}, timestamp)`；帧分辨率不进 metadata，在 `FrameDetection.frame_width/height`。
- **Operator**（流算子，规则粒度，per-run 独立）：`name`、`subscribes`（**显式、必填**输入流名列表，缺则 fail-fast）、`window_seconds`；`analyze(windows: List[FrameDetection])` 推进 `self._sm`、`judge() → (overlay_texts, alarms)`、`finalize() → List[Alarm]`（结算，默认空）。analyze+judge **合并**进单个 Operator（单对象内完成，不产 `TemporalEvent` 作为对象间传输）。`windows` 是帧级 `FrameDetection` 快照（多流已对齐进 `by_source`），算子内 `_clip` 到自身感受野，单订阅用 `primary_window(windows) → List[DetectorOutput]` 投影自身流。
- **TemporalOperator**（`Operator` 子基类，供动作识别时序模型）：多带 `model_path` / `objects` / `actions` / `model_input_fps` 四参。`model_input_fps` 必填、>0、且 ≤ `settings.inference_fps`（相对容差 1e-9；重采样只能降采样，契约帧率高于检测采样率即构造期 `ValueError`）。惰性 `torch.jit.load`（双检锁、缺文件 `FileNotFoundError`、失败 `_load_failed` 锁存），CPU 上 `infer(features)` 前向出 logits。`_resample_by_ts` 一行委托共享 `resample_by_ts`（宽松模式）。子类 `CleanOperator`（`online/temporal/impl/clean.py`）在 `analyze` 内**先按帧 ts 重采样**到 `model_input_fps`（15fps 10s 窗口 → 7.5fps），再 `_adapt_to_features` 成 `(T, num_objects×6)` 张量（每物体 `(count,cx,cy,w,h,area)`，异常帧留全零行保持时间轴不缺帧）后 `infer`，取末步 argmax 存 `_sm['latest_action']`；`judge` 仅出 overlay 文案、当前不产告警。新帧门 / `last_ts` 推进仍基于**完整**窗口，重采样只定喂模型的时间轴密度——隔开「检测密度（`inference_decimation`）」与「模型入模节奏（`model_input_fps` 契约）」，消除 train/serve fps skew。这是 CLEAN stage 的**在线**时序算子（YAML `clean_monitor`，`gru-final.pt`，`window_seconds=10`，`model_input_fps=7.5`），与离线 CLEAN segmenter 是两条独立链路。

## 按 ts 重采样（共享纯函数 `resample.py`）

`resample_by_ts(frames, fps, *, strict=False) → List[FrameDetection]`，在线 `TemporalOperator` 与离线 Segmenter 共用（离线不得 import online，故放共享层）：

- **相位网格抽稀**：从首帧起 `next_t += 1/fps`，保留首个 `ts >= next_t - tol` 的帧。`tol` = 半个输入帧间隔（相邻正 ts 差的中位数），吸收 ts 舍入 / 抖动，整数比下保留间隔严格均匀（15→7.5fps 间隔恒为 2 帧）。网格前进（非从上一保留帧累加）不累积漂移；缺口令网格落后当前帧时重锚到当前帧，不追补突发。
- **只降采样、不合成帧**：纯 ts 函数，不改帧内容；帧数 < 2 原样返回副本。
- `strict=True`：输入帧率低于 `fps`（超 1% 余量）抛 `ValueError`。在线用宽松模式（慢输入原样放行，由 `TemporalOperator` 构造期上界校验兜）；离线 `CleanNodepGRUSegmenter.preprocess` 用 `strict=True`。
- 在线 `gru-final.pt` 的入模帧因容差修正向训练口径靠拢，线上动作结果预期会变化（待核验：无线上观测记录）。

## L3/L4：ClientTemporalActor（2Hz）

per-run daemon 线程（`guarded_run` 包裹），`tick_interval=0.5s`（2Hz，全局唯一 tick 真源，构造时不覆盖）。每 tick 取一次帧窗 `windows = cq.get_slide_window()`（`List[FrameDetection]`），对每个 operator：`op.analyze(windows)`（算子内自行 `_clip` + 按 `subscribes` 投影）→ `op.judge()` 收 (events, alarms)；汇总后 `cq.set_latest_temporal(events)`，告警烧 `alarm.stage` 别名后 `alarm_sink.persist_alarms(alarms, cq=cq, mode=REALTIME)`。**per-operator 隔离**（单算子异常不断整 tick）。stage 别名在构造期解析一次，是全仓唯一告警路径 alias 解析点。停机两段：`signal_stop()` → `finalize_and_stop() → List[Alarm]`（join 2s；线程未退则跳过结算以免 `_sm` 并发读写；否则收各 operator `finalize()`）。

## 告警落库：alarm_sink.persist_alarms

`persist_alarms(alarms, *, cq, mode, log_each=False)`：从 CQ 取 `task_id/step_id/source_ip`；逐条设 `alarm.mode`，`cq.append_alarm_record_with_gate(alarm, mode)` 过 5s 冷却闸 + 入环形日志，再 `alarm_service.persist_alarm(...)` 入告警上报队列。**过闸编排归推理产出域，alarm 服务只做无状态上报**（见 [SERVICE_ALARM.md](SERVICE_ALARM.md)）。别名由 Actor 先烧好，sink 直读 `alarm.stage`。结算告警的调用方：per-run 拆除时是 `RunControlService.stop_run`（拿 `stop_workflow` 返回值），进程停机时是 `InferenceService.stop()`。

## online / offline 分离

```text
在线  L1 写回 → cq._slide_window → Actor 2Hz → Operator(_sm) → overlay / 告警
         └──→ cq.ca_detections → recording → {run}/inference/detections.jsonl
离线  inference.read_detections(run) → OfflineSegmenter → temporal.jsonl（TemporalSegment）+ label_probs.npz
```

两链**彻底分离**：在线不写 `temporal.jsonl`、Actor 不读事实；离线不接 CQ / 在线 Operator / 告警 / DB。共用货币是 `FrameDetection`：在线写回口入落盘缓冲，离线 `read_detections(run)` 一次读回（按 ts 升序、多流已对齐）喂 `OfflineSegmenter.preprocess(frames)`。离线入口两条：CLI 手动跑，或 admin 经 `OfflineJobService` 提交（起的也是同一个 CLI 子进程）。离线结果的读口：`POST /ai/temporal`（分段 → 媒体毫秒）、`POST /lab-f3m8/label-probs`（逐帧概率曲线）、`GET /lab-f3m8/tasks` 行内 `offline_steps`（`query_has_offline_results`）；端点契约见 `docs/api/`。离线不判合规、不产告警、不入库；run 结束后自动触发未实现。

## 离线段（`offline/`）

### 策略基类 `OfflineSegmenter`

一策略 = 一个 `OfflineSegmenter` 子类（自包含单文件，实现不散落框架层）。基类不定义 `__init__`，构造参数全部来自 YAML `offline.params`（`cls(**params)`）；`name` 是只读 property = `type(self).__name__`，即产出 `TemporalSegment.producer`。抽象方法 `preprocess(frames: Sequence[FrameDetection]) → Any`（无默认特征工程）与 `segment(model_input) → List[TemporalSegment]`；可选旁路 `label_probs() → LabelProbs | None`（默认 None，产逐帧概率的模型 override）。策略是纯算法：不碰 `app.storage` / ClientService / CQ / DB，`frames` 只读。

### `OfflineRunner`：一次运行的编排

`OfflineRunner.run(OfflineRunSpec(task_id, step_id, run_id=None)) → OfflineRunResult`（`runner.py`）：

```text
config.require_offline(step_id)          未定义 / offline 为空 → ValidationError（无兜底）
StageFactory.create_offline_segmenter    producer = 类名
runs.query(task, step, run_id)           入口解析一次 run 并锁定，之后全程只读写它
    点名的 run 不在 → reclaimed；缺省且无可见 run → skipped
inference.read_detections(run)           为空 → skipped（不覆盖旧结果）
preprocess → segment                     算法异常上抛，不写
_validate                                isinstance / producer==name / 有限数 / start<=end / 0<=conf<=1；任一非法整批失败
按 (start, end, label) 排序
① _maybe_write_label_probs               label_probs() 非 None → 形状检查 → write_label_probs
② _replace_segments                      read_temporal → 丢全部旧 TemporalSegment、保留 TemporalEvent → + 本次 → write_temporal
    ①② 中 FileNotFoundError 且 runs.query 确认 run 已不在 → reclaimed（不重建目录）
→ completed
```

- 状态全集 `{completed, skipped, reclaimed}`；其余失败以异常上抛。`producer` 只校验不盖章——策略填错名字当场报错。
- **读-合并-写**：`write_temporal` 是整体替换，盲写会吃掉 `TemporalEvent`，合并规则（保留谁）是离线语义，归 Runner 而非存储层。合并时丢弃**全部**旧 `TemporalSegment`（不分 producer）：一个 stage 至多一个离线模型，换模型重跑时旧类名的分段整体被替换；空结果 = 清除该 run 的分段。
- **旁路先于事实**：事实落盘即代表本次运行完成；旁路在前，页面读到新事实时概率必然已是同一次运行的。旁路形状不一致或非 `FileNotFoundError` 的写失败只 warning、不落，事实照写。策略不产概率时不动旧 `label_probs.npz`（换成不产概率的模型重跑会残留旧概率）。
- run 锁定在入口：运行期间同 step 重启，结果仍写回解析出的那个 run；「该 run 正在运行」由作业服务提交时挡（409）。同一 run 跨进程并发跑离线无互斥（read-modify-write，一期不支持）。
- 不收 `base_dir`（存储根归 settings）；`config` 可注入（测试）。

### CLI

```text
python -m app.services.inference.offline.cli run   --task-id T --step-id S [--run-id R] [--threads K]
python -m app.services.inference.offline.cli query --task-id T --step-id S [--run-id R] [--producer P]
```

- `run` 四参数：`--run-id` 缺省 = 该 step 最新可见 run；`--threads` 默认 2。先 `_isolate_cpu`（`CUDA_VISIBLE_DEVICES=""` + `torch.set_num_threads`）再 import runner/策略，保证 torch 首次 import 时已看不到 GPU。
- **stdout 末行固定一行 JSON** `{status, producer, segment_count, message}`（`ensure_ascii=False`），人读与作业服务共用。completed / skipped / reclaimed → 退出码 0；step 未配置 / 输入损坏 / 策略异常 / 写失败 → `status="error"`、退出码 1。
- `query` 只读 `temporal.jsonl` 的 `TemporalSegment`（可按 `--producer` 过滤），输出 `timeline`，不碰 torch/runner。
- 无策略覆盖参数：换策略只能改 YAML（`class` 与 `params` 一起换）。

### 作业服务 `OfflineJobService`

`offline/service.py`，单例 `offline_job_service`（`offline/instance.py`），admin 路由直接持有：

- **执行模型**：一条 `SerialTaskQueue("offline", maxsize=QUEUE_SIZE)`，一次只跑一个作业；每个作业起子进程 `[nice -n 15] python -m …offline.cli run --task-id --step-id --run-id --threads 2`，解析 stdout 末行 JSON。选子进程而非进程内调 runner：`torch.set_num_threads` 作用于整个进程会影响在线，且进程内无法中途 kill。
- **子进程环境**：`os.environ` 副本（同一存储根）+ `CUDA_VISIBLE_DEVICES=""` + `PYTHONIOENCODING=utf-8`，`cwd` = 仓库根；降优先级 Windows `BELOW_NORMAL_PRIORITY_CLASS` / POSIX `nice -n 15` 命令前缀（不用 `preexec_fn`，多线程进程里不安全）；stdout/stderr 写临时文件不用管道（防输出多时阻塞）。
- **常量**：`THREADS=2`、`QUEUE_SIZE=20`、`JOB_TIMEOUT_S=1800`、`HISTORY=200`、`POLL_S=1`。
- **提交 `submit(run: RunIdentity)`**（run 由调用方经 `runs.query` 解析后传入，作业锁定这一个 run）：`require_offline` → 400；服务未启动 → 409；`clients.get(task).run == run`（该 run 就是当前注册 CQ 的 run，输入还在写）→ 409；同一 run 已有在途作业 → 返回在途那个（**按 `RunIdentity` 去重**，不同 run 各自一个作业，同 step 的旧 run 照常接收）；队满 → 409。
- **状态**：`queued / running / completed / skipped / reclaimed / failed / cancelled`。退出码 0 且 JSON status ∈ {completed, skipped, reclaimed} 才透传，否则 failed（message 优先取 JSON，没有再取 stderr 尾 2000 字符）。
- **监视**：`_watch` 每 `POLL_S` wait 一次，只管超时（超时 kill → failed）；运行中不查 live、不因换代 kill。
- **取消 / 停机**：`cancel(run)` 排队中直接 cancelled、运行中 kill；起子进程与取消 kill 在同一把锁下，不漏刚起的子进程。`stop()` 先 kill 运行中的，排队中的在队列排空时逐个记 cancelled。
- **状态只在内存**，重启丢失（结果本身已落盘）；已结束作业保留最近 200 条。`launcher` / `config` / `clients` 仅供测试注入。
- **生命周期**：`inference/__init__.py::lifespan()` 里在线 `inference_service` 先起、`offline_job_service` 后起；停机时离线**先停**（先 kill 子进程，不与在线收尾抢 CPU）。

### CLEAN 离线策略（`offline/impl/clean.py`）

一文件自包含：特征工程为模块级纯函数、四个模型类、解码逻辑。基类 `_CleanTorchSegmenter`：`segment` 逐帧 softmax `[T,C]` → argmax →「背景类 idle 断开 → 同类合并 → 丢弃短于 `min_duration_s`」→ `TemporalSegment`（`meta.model_version`），同时缓存 `LabelProbs(ts=降采样后帧 ts, probs, labels)` 供 `label_probs()`。未配 `model_path` → `ValueError`（**不做规则降级**）；权重缺失 → `FileNotFoundError`。特征矩阵有 `NaN/inf→0` 兜底。

- **默认模型 `CleanNodepGRUSegmenter`**（YAML 当前启用）：
  - 特征 `ama-v3-concat23-nodep-226d`：v2（113）⊕ v3（113，scope 器械轴坐标系），读入层丢弃废弃类 `scope_distal_end / short_brush / long_brush`。
  - `preprocess`：`resample_by_ts(frames, model_input_fps, strict=True)`（只挑真实帧、ts 与 `detections.jsonl` 位级相等；检测帧率低于契约帧率即 `ValueError`）→ `build_nodep_concat_features(..., confidence_override)`。
  - 推理：因果滑窗，每帧取以它为末帧的 16 帧窗口 `[T,16,226]`（开头不足一窗用首帧重复补齐），单向 3 层 GRU(hidden=128) + Linear 取窗末输出，分批 1024 前向 → softmax `[T,6]`；类别 `idle / water_injection / flush / long_brush_insert / long_brush_withdraw / short_brush_cleaning`。
  - **单文件加载**：只读 `.pt` 的 `model_state`（`weights_only=True`、`strict=True`），不读训练框架旁挂的 `.meta.json`；hidden / 层数 / window 固定在策略类，换训练配置须同步改类。
  - 构造约束：`model_input_fps` 必填（>0）；`confidence_override` 缺省 None（用真实置信度），须在 [0,1]。
  - **静默风险（配错不报错）**：window / 特征版本无运行时校验；`model_input_fps` / `confidence_override` 须与训练口径一致（当前取值的训练口径待核验）；特征按整段计算（非因果），不能原样搬到在线。
- **三个整段双向模型**（备选，YAML 注释里给出类 ↔ 权重映射）：`CleanMSTCNBiLSTMSegmenter`（base v2 113 维）、`CleanASFormerSegmenter`（+business_priors 121 维）、`CleanBiGRUSegmenter`（+window_stats+business_priors 249 维）；多态只在各子类 override `preprocess`。权重 `torch.load(weights_only=False)`（checkpoint 含 numpy normalizer）+ `strict=True`，并校验 checkpoint `feature_version` / `feature_names` 与后端输入一致，不一致即 `ValueError`。只收 `model_path` / `min_duration_s`。
- **权重命名约定** `clean-offline-<模型>.pt`（`gru-nodep` / `bigru` / `asformer` / `mstcn`），放 `${CLEANSIGHT_MODEL_PATH:./app/data}`；训练/导出在独立 `offline-model` 仓，`.pt` 不入后端仓。在线进程从不实例化 `offline` 块的类：类路径错、权重缺失只让该离线作业 failed，不影响在线。

## 推理链路压力观测（[PRESSURE] / [VIZ_THROUGHPUT]）

诊断日志契约统一见 [DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)（`[PRESSURE]`/`[VIZ_THROUGHPUT]`/`[BACKPRESSURE]` 三条正交、仅压力时打印、平稳静默）。inference 侧的落点：`StageAwareDispatcher` 在调度循环里每 ~1s 采样自己的 stage deque，经 `PressureReporter` 打 `[PRESSURE] resource=stage_queue`（每资源单一上报者，`ca_processed` 交回 `ClientQueues` 自报、不越权汇总）；`_stage_drops` 记 deque 满淘汰、`_stage_rejects` 记 `submit()` 返 False 的下游拒收，二者并入压力行（`reject_delta>0` 即「下游在拒收」判据）。`VisualizationWorker` 另打 `[VIZ_THROUGHPUT]` 量成帧速率亏空，自动三侧归因（`viz-starved > render-bound > supply-bound`）。

## Stage 配置与当前阶段

`config/inference_config.yaml`，每 stage 三段 `detectors[]` / `rules[]` / `offline`，主键 = step_id 字符串，`alias` = 可读名。当前只有两个 stage，**没有兜底 stage**：

- `"1"` / alias `LEAK`：detectors `bubble` + `bending`；rules `bubble_leak`（realtime）、`bending_check`（settlement）；`offline: {}`（不可跑离线）。
- `"2"` / alias `CLEAN`：detectors `clean_large` + `clean_small`；rule `clean_monitor`（`CleanOperator`，订阅两流，`gru-final.pt` 在线动作识别，realtime）；`offline.class = …offline.impl.clean.CleanNodepGRUSegmenter`，`params = {model_path: …/clean-offline-gru-nodep.pt, model_input_fps: 7.5, confidence_override: 1.0, min_duration_s: 0.2}`。

`offline` 块形状 `{class, params}`：缺省或 `{}` = 该 stage 不可跑离线；非空时 `class` 必填可导入（`StageFactory.create_offline_segmenter`），无 `name` / `subscribes` / `enabled`。校验口：在线 `InferenceService.resolve_stage`（`/api/start`，400）、离线 `InferenceConfig.require_offline`（作业提交 400 / CLI 退出码 1），都无兜底。

跨模块真旋钮（`raw_fps`/`inference_decimation`）与时间概念（`ca_*_seconds`）在 `app/settings.py` 单一真源（`inference_fps`/`ca_maxlen` 等为其派生量，见 [SERVICE_CONFIG.md](SERVICE_CONFIG.md) 三层模型）；本 YAML 只留 `batch_size` 等推理自有参数 + `model_input_fps` **模型契约**（在线 CleanOperator 与离线 Nodep 策略的 `params`，随产物钉死、必填、构造期校验，模型侧按 ts 重采样入模）。

## naming.py 运行时注册表

`online/naming.py`，由 `InferenceService.start()` 经 `StageFactory` 初始化（单测缺失时惰性回落 YAML）：

- `_TASK_METRIC_MAP: {detector_name → AlarmMetric}`（仅 `realtime:true` 规则的流），供 signals_10s。
- `_STAGE_ALIAS_MAP: {stage_key → alias}`（如 `"1"→"LEAK"`），落告警 `stage` 字段 + 可视化叠字。

## 代码来源

- `app/services/inference/__init__.py`（`lifespan`）、`{config,stage_factory,resample}.py`
- `app/services/inference/online/{service,instance,naming,render,types}.py`
- `app/services/inference/online/detection/{detector,dispatcher,infer_proxy,stage_worker,service}.py`
- `app/services/inference/online/temporal/{operator,actor,alarm_sink}.py`（`operator.py` 含 `Operator` + `TemporalOperator`）
- `app/services/inference/online/visualization/{visualization_worker,visualizer}.py`（`visualization_worker.py` 含 `[VIZ_THROUGHPUT]`）
- `app/services/inference/online/{detection,temporal}/impl/{bubble,bending,clean}.py`
- `app/services/inference/offline/{segmenter,runner,cli,service,instance}.py`、`offline/impl/clean.py`
- `app/types/{detection,temporal,run}.py`、`app/storage/inference/`
- `config/inference_config.yaml`
- `tests/test_infer_proxy.py`、`tests/test_inference_stage_routing.py`、`tests/test_writeback_handle_fence.py`、`tests/test_inference_resample.py`、`tests/test_offline_pipeline.py`、`tests/test_offline_job_service.py`、`tests/doubles.py`、`integration_tests/test_offline_job_subprocess.py`
