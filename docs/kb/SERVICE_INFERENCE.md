> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Inference Service

推理服务分两段：**在线段**负责 stage 路由、L1 目标检测、L3/L4 时序分析与判定、可视化和告警过闸；**离线段**在 run 结束后对整段检测序列做动作分割。检测结果与离线产物的格式归 `app.storage.inference`，在线检测结果的落盘编排归 recording。检测标准与阈值见 [BUSINESS_DETECTION_STANDARDS.md](BUSINESS_DETECTION_STANDARDS.md)，链路原则见 [DESIGN_DETECTION_WORKFLOW.md](DESIGN_DETECTION_WORKFLOW.md)。

## 包结构：online / offline 两段加一层共享

```text
app/services/inference/
  __init__.py         lifespan()：inference_service 先起后停，offline_job_service 后起先停
  config.py           InferenceConfig / StageConfig；require_offline(step_id)
  stage_factory.py    StageFactory：建 Detector / Operator spec / OfflineSegmenter，建 metric 与别名表
  resample.py         resample_by_ts（在线、离线共用）
  online/
    service.py        InferenceService（生命周期、resolve_stage、per-run workflow）
    instance.py       单例 inference_service
    naming.py / types.py(DetectionTask) / render.py(RenderSpec)
    detection/        detector(Detector, YOLODetector) / dispatcher(StageAwareDispatcher)
                      infer_proxy(RemoteInferProxy) / stage_worker(StageWorker, run_stages)
                      service(DetectionService) / impl/{bubble,bending,clean}
    temporal/         operator(Operator, TemporalOperator) / actor(ClientTemporalActor)
                      alarm_sink(persist_alarms) / impl/{bubble,bending,clean}
    visualization/    VisualizationWorkerPool / FixedVisualizer（写 ca_processed + _latest_rendered）
  offline/
    segmenter.py / runner.py / cli.py / service.py(OfflineJobService) / instance.py
    impl/clean.py     CLEAN 四个策略，默认 CleanNodepGRUSegmenter
```

- **online 与 offline 运行期互不 import**，共用的只能放顶层。`offline/cli.py` 不得被包内任何模块 import；`offline/service.py` 不 import runner / torch，只把 CLI 当子进程命令。
- 包 `__init__` 零 re-export，消费方走深路径。测试替身 `MockDetector` / `BrushRulesSegmenter` 在 `tests/doubles.py`。

## InferenceService：构造零副作用，重组件在 start() 里建

单例在 `online/instance.py` 模块级构造，所以构造只建空容器；stage 配置、`DetectionService`、可视化池都在 `start()` 的 `_build_components` 里建，否则 import 本包就会经 importlib 拉起全部 impl 与 torch。

- `start()`：起 `DetectionService`（proxy + dispatcher）与可视化池，初始化 naming 表。
- `stop()`：停 DetectionService → 两阶段停残留 actor（全部 `signal_stop`，再逐个 `finalize_and_stop` 并落结算告警）→ 停可视化池。正常停机时 `run_control.lifespan`（嵌在 `inference.lifespan` 里层）已先对每个在跑 run 调 `stop_run`，actor 已被摘空，这里只是兜底。
- `start_workflow(cq)`：入参是 RunControlService 已注册的 CQ，本方法不碰注册表和存储。按 `cq.stage` **逐 run 实例化** operators，按订阅流的最大 `window_seconds` 调 `cq.set_stream_windows`，起 `ClientTemporalActor`；stage 无 rule 则不建 actor。
- `stop_workflow(cq) → List[Alarm]`：pop actor，收结算告警返回给 RunControlService 落库。不收尾检测结果，剩余缓冲由 RunControlService 随后调 `recording.flush_residual(cq)` 交出。
- `resolve_stage(step_id) → str`：恒等路由，无兜底。`str(step_id)` 是有 detector 的 stage 则原样返回，否则 `ValidationError`（400）。RunControlService 在锁外、动旧 run 之前调用；入参是 int，故 `"02"` 与 `2` 对到同一 stage。
- 两个 workflow 方法的互斥由 RunControlService 的 `lock_for(task_id)` 承担。

**配置错误的暴露时机分两种**：

- 启动期：detector 导入或构造失败、rule 缺 `class` / `subscribes`、operator 类导入失败、没有任何生效 stage → `RuntimeError` 冒到 lifespan，后端起不来。没配 detector 的 stage 只记日志、不生效。
- 每次 `/api/start`：operator 在 `start_workflow` 里才构造，构造期校验（如 `TemporalOperator` 的 `model_input_fps` 上界、`params` 多余或缺失键）失败时，该 run 回滚、请求报错，后端照常运行。

运行时推理失败走逐帧降级（见「L1 写回」），从不切 stage。

## L1：GPU 前向在 spawn 子进程，dispatcher 是唯一提交者

主进程各线程抢 GIL 会饿死 kernel 发射线程（隔离实验定案），所以 GPU 前向放在独立 spawn 子进程（`stage_worker.run_stages`），推理循环独占一把 GIL。

- **主进程无 CUDA context**：主进程的 detector 实例只供 viz 调 `prepare_visualization_data`，不加载模型；在线时序模型钉 CPU。子进程先钉 `CUDA_VISIBLE_DEVICES`，再从 YAML 自建 `{stage: StageWorker}`，warmup 时加载权重。必须 spawn（CUDA + fork 不安全）。
- **peek-commit 轮转排空**：`_fetch_and_dispatch_round` 从各 run `pop_ca_ready()` 取一帧，连同 CQ 句柄进 `_stage_queues[stage]`（`deque(maxlen=256)`，满则淘汰最旧帧，计 `infer_backlog`）；`_drain_and_submit` 每圈每 stage peek 一批 → `submit`，接了才 `_commit_pop`，被拒即 return（帧留 deque），整圈无进展即停。每 stage 每圈一批，保证公平。
- **dispatcher↔proxy 只有 `submit` 一条布尔背压接口**：`inflight >= max_inflight(8)` 即返 False，proxy 不外泄计数。
- **cq 句柄不过进程边界**：`submit` 在 `pending[req_id]` 存 `_Pending(cq, timestamp, frame_width, frame_height)`（帧分辨率唯一采集点）后丢弃帧引用；子进程只跑纯数据的 `_infer_models(frames, timestamps)`；collector 按 req_id `pending.pop` 组装 `FrameDetection` 回调写回。单 `req_q` FIFO，同 stage 保序；Prometheus 埋点在主进程。
- **背压反馈接缝未接通**：`_admit_to_stage` 恒返回 True，`_stage_backpressure` 预留未用。

## RemoteInferProxy：三条监督判据防静默不可用

- **防泄漏**：pending 有界（`max_inflight=8`，满即拒收）；collector 每条响应 `pending.pop`；子进程失败时清空 pending、`inflight` 归零、退避重 spawn。迟到或跨 run 的结果由写回口的 `cq.is_active()` 挡掉。
- **监督三判据**（`_supervise_loop`，1s tick）：`dead`（进程死）、`wedged`（`inflight > 0` 且超 `response_timeout` 无响应）、`not_ready`（`_poll_readiness`：活着但没就绪）。第三条不能省：`_spawn_child` 只等一次 `ready_ev`（`ready_timeout=120s`），warmup 超时后子进程活着、submit 全被拒、inflight 恒 0，前两条同时哑火，全链路永久 0 推理。`_poll_readiness` 先补收迟到的就绪信号（不重来、省一次模型加载），仍超 `ready_timeout` 才判失败走 `_handle_child_failure`。
- 推理链上的 `frame_drop_total` reason：`infer_backlog`（dispatcher deque 满）、`infer_child_down`（`stop` 时未排空）、`infer_child_restart`（重启清孤儿）、`stale_run`（写回口迟到结果）。

## 子进程收尸：队列三步缺一步就挂死进程退出

`multiprocessing` 的 atexit 钩子会无超时 `join` 主进程侧的 `QueueFeederThread`。子进程 wedge 时不读 `req_q`，MB 级帧批灌满 64KB 管道，feeder 阻塞在 `send_bytes`，uvicorn 退出后进程仍挂着。`q.close()` 不关本进程持有的读端 fd，救不了。`_kill_child` 因此按序做三步：

1. `req_q._reader.close()`：阻塞的写立刻 EPIPE，feeder 自退。只对 `req_q` 做，`resp_q` 读端 collector 在用。
2. 两个队列都 `cancel_join_thread()`：子进程杀不死（CUDA D 态）时 EPIPE 不来，只能靠注销 atexit join。
3. `close()`。

`_spawn_child` 须在首次 put 前置 `req_q._ignore_epipe = True`，否则每次收尸都向 stderr 打 `BrokenPipeError`。

## 检测契约与时序契约（`app/types/`）

字段定义、时间轴约束与落盘格式归 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)，本服务只关心三点：

```text
L1   app/types/detection.py      DetBox → DetectorOutput → FrameDetection     detections.jsonl
L3   app/types/temporal.py       TemporalEvent / TemporalSegment；LabelProbs   temporal.jsonl；label_probs.npz
```

- **`FrameDetection` 是唯一的帧级检测对象**（`ts / by_source{流名: DetectorOutput} / frame_width / frame_height / cq`）：collector 组装、写回口分发、落盘、离线回读都是它。`cq` 只在 collector → 写回口这一段有值，留存的帧一律不带。
- **时序 ts 是帧捕获墙钟浮点秒**，与 `FrameDetection.ts`、HLS `.idx` 同源同值，是「时间一律 int 毫秒」的具名例外；对外出口换算成媒体毫秒。
- `TemporalEvent` 当前零生产者（在线 Operator 的状态留在 `_sm`）；`LabelProbs` 只是可视化旁路，不参与任何判断。

## L1 写回：单入口、句柄化、零 IO

`DetectionService._write_back_results(List[FrameDetection])` 是 L1 唯一写回口，作 `write_back=` 注入 proxy，由 collector 线程回调。逐帧：

1. `cq, frame.cq = frame.cq, None`：取走句柄并清空，避免 cq → 帧窗 → 帧 → cq 的引用环。
2. `cq.is_active()` 为假（DRAINING / CLOSED）→ 整帧丢弃，计 `stale_run`。
3. 同一对象三处投递（不复制）：
   - `cq.push_detection(frame)` → 帧窗 `_slide_window`，供 L3；
   - `cq.set_latest_detection(frame)` → 原子快照，供 Viz；
   - `cq.append_ca_detections(frame)` → 落盘缓冲。**降级帧不入落盘缓冲**：任一源 `success=False` 的帧照写帧窗与快照（画面照常、没框、逐源 warning），但不落盘——落盘格式不带 `success`，空框会被离线当成「没检出」。

写回线程不碰盘，全程用 dispatcher 捕获的 CQ 句柄，无 `client_id` 反查。三个写入口自身也各有 ACTIVE 门。

## 推理产物落盘：本服务不持 store

三份产物都在 run 目录 `{root}/{task_id}/{step_id}/{run_id}/inference/` 下（写法与读者见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)）：

- `detections.jsonl`：**拉模式**，写回口只碰 cq，recording sweeper 每 tick `drain_ca_detections` 后经 `recording-detections` 队列追加；拆 run 时 `flush_residual(cq)` 全排空（见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)）。因此不存在 inference → recording 的依赖边。
- `temporal.jsonl` / `label_probs.npz`：只由离线 `OfflineRunner` 整体原子替换。
- 停机：`run_control.lifespan` 最先退出，逐个 `stop_run`，剩余检测结果经 `flush_residual` 交给仍活着的 recording 队列（`recording.lifespan` 在 `inference.lifespan` 外层）排空落盘。

## Detector / Operator 框架接口

- **Detector**（流源，无状态，多 run 共享）：`name` = 产出流名 = `by_source` 的 key；`infer_batch(frames, timestamps) → List[DetectorOutput]` 是唯一推理入口；`prepare_visualization_data(output) → RenderSpec`。`timestamps[i]` 是帧捕获真值，须原样写入对应 `DetectorOutput.timestamp`，detector 不得自造时间戳。`YOLODetector` 提供惰性加载（双检锁）、批量 predict、输出适配（`metadata={"model": "yolo"}`），整批异常时逐帧返回 `success=False` 的空结果并保留各帧 ts。`class_name` 直接取模型 `result.names`，不归一化。
- **Operator**（流算子，一条规则一个，per-run 实例）：构造参数 `name` / `subscribes`（必填，空即 `ValueError`）/ `window_seconds`；`analyze(windows: List[FrameDetection])` 推进 `self._sm`；`judge() → (overlay_texts, alarms)`；`finalize() → List[Alarm]`（结算，默认空）。基类工具 `_clip(windows)` 裁到感受野、`primary_window(windows) → List[DetectorOutput]` 投影首个订阅流。
- **TemporalOperator**（`Operator` 子类，承载时序模型）：多四个参数 `model_path` / `objects` / `actions` / `model_input_fps`。`model_input_fps` 必填、> 0、≤ `settings.inference_fps`（相对容差 1e-9；重采样只能降采样）。模型惰性 `torch.jit.load`（双检锁，缺文件 `FileNotFoundError`，失败后 `_load_failed` 锁存），CPU 上 `infer(features)` 出 logits；`_resample_by_ts` 委托共享 `resample_by_ts`（宽松模式）。
- **CleanOperator**（rule `clean_monitor`）：`analyze` 先 `_clip`，有新帧时把窗口重采样到 `model_input_fps`，`_adapt_to_features` 成 `(T, num_objects×6)` 张量（每物体 `count, cx, cy, w, h, area`；异常帧留全零行，时间轴不缺帧），前向后取末步 argmax 存 `_sm['latest_action']`；`judge` 只出叠字。新帧门与 `last_ts` 基于完整窗口，重采样只决定喂模型的时间密度。与离线 CLEAN 策略互相独立。

## resample_by_ts：相位网格抽稀，只降采样

`resample_by_ts(frames, fps, *, strict=False)`，在线 `TemporalOperator` 与离线策略共用，所以放共享层。

- 从首帧起 `next_t += 1/fps`，保留首个 `ts >= next_t - tol` 的帧；`tol` = 相邻正 ts 差中位数的一半，吸收舍入与抖动，整数比下间隔严格均匀（15→7.5fps 恒隔 2 帧）。网格前进而非从上一保留帧累加，不累积漂移；缺口使网格落后时重锚到当前帧，不追补。
- 不改帧内容、不合成帧；帧数 < 2 原样返回。
- `strict=True`：输入帧率低于 `fps` 超 1% 即 `ValueError`。在线用宽松模式（上界由 `TemporalOperator` 构造期校验），离线 `CleanNodepGRUSegmenter` 用严格模式。

## ClientTemporalActor：per-run 2Hz tick

per-run daemon 线程（`guarded_run` 包裹），`tick_interval=0.5s` 是全局唯一 tick 真源。每 tick 取一次 `cq.get_slide_window()`，对每个 operator 依次 `analyze` → `judge`，**单算子异常不影响同 tick 其余算子**；汇总 overlay 写 `cq.set_latest_temporal`，告警烧入 stage 别名后经 `alarm_sink.persist_alarms(mode=REALTIME)` 落库。stage 别名在构造期解析一次，是全仓唯一的告警别名解析点。

停止分两步：`signal_stop()` → `finalize_and_stop()`（join 2s；线程未退出则跳过结算，避免 `_sm` 并发读写；否则收集各 operator 的 `finalize()`，同样烧别名）。

## alarm_sink：过闸编排归推理产出域

`persist_alarms(alarms, *, cq, mode, log_each=False)`：从 cq 取 `task_id / step_id / source_ip`，逐条设 `alarm.mode`，经 `cq.append_alarm_record_with_gate` 过 5s 冷却闸并入环形日志，通过的再 `alarm_service.persist_alarm(...)` 入上报队列。alarm 服务只做无状态上报（见 [SERVICE_ALARM.md](SERVICE_ALARM.md)）。结算告警的调用方是 `RunControlService.stop_run`（含停机时的 `shutdown`），`InferenceService.stop()` 只兜残留 actor。

## 在线与离线彻底分离，共用货币是 FrameDetection

离线段从 `inference.read_detections(run)` 读回整段 `FrameDetection`（按 ts 升序、多流已对齐），产 `TemporalSegment` 与 `LabelProbs`。在线不写 `temporal.jsonl`；离线不接 CQ、在线 Operator、告警、DB，不判合规。入口两条：CLI 手动跑，或 admin 经 `OfflineJobService` 提交（同样起 CLI 子进程）；run 结束后自动触发未实现。读口：`POST /ai/temporal`、`POST /lab-f3m8/label-probs`、`GET /lab-f3m8/tasks` 行内 `offline_steps`，契约见 `docs/api/`。

### OfflineSegmenter：一个策略一个自包含子类

构造参数全部来自 YAML `offline.params`（`cls(**params)`，基类不定义 `__init__`）；`name` 是只读 property = 类名 = 产出的 `TemporalSegment.producer`。抽象方法 `preprocess(frames) → Any`（无默认特征工程）与 `segment(model_input) → List[TemporalSegment]`；可选 `label_probs() → LabelProbs | None`。策略是纯算法：不碰 `app.storage` / ClientService / CQ / DB，`frames` 只读。

### OfflineRunner：锁定一个 run，先旁路后事实

`OfflineRunner.run(OfflineRunSpec(task_id, step_id, run_id=None)) → OfflineRunResult`：

```text
config.require_offline(step_id)          未定义 / offline 为空 → ValidationError
StageFactory.create_offline_segmenter    producer = 类名
runs.query(task, step, run_id)           入口解析一次 run，之后只读写它
    点名的 run 不在 → reclaimed；缺省且无可见 run → skipped
inference.read_detections(run)           为空 → skipped（不覆盖旧结果）
preprocess → segment                     算法异常上抛，不写
_validate                                类型 / producer==name / 有限数 / start<=end / 0<=conf<=1；任一非法整批失败
按 (start, end, label) 排序
① _maybe_write_label_probs               label_probs() 非 None → 形状检查 → write_label_probs
② _replace_segments                      read_temporal → 丢全部旧 TemporalSegment、保留 TemporalEvent → + 本次 → write_temporal
    ①② 抛 FileNotFoundError 且 run 已不在 → reclaimed（不重建目录）
→ completed
```

- 状态全集 `{completed, skipped, reclaimed}`，其余失败以异常上抛。`producer` 只校验不盖章。
- 合并规则归 Runner（`write_temporal` 是整体替换）。丢弃**全部**旧分段、不分 producer：一个 stage 至多一个离线模型，换模型重跑即整体替换；空结果 = 清除该 run 的分段。
- 旁路先写，页面读到新事实时概率必然同源。旁路形状不一致或非 `FileNotFoundError` 的写失败只 warning。策略不产概率时旧 `label_probs.npz` 不动，会残留。
- 同一 run 跨进程并发跑离线无互斥，一期不支持；「该 run 正在运行」由作业服务提交时挡。

### CLI：stdout 末行固定一行 JSON

```text
python -m app.services.inference.offline.cli run   --task-id T --step-id S [--run-id R] [--threads K]
python -m app.services.inference.offline.cli query --task-id T --step-id S [--run-id R] [--producer P]
```

- `run`：`--run-id` 缺省 = 该 step 最新可见 run，`--threads` 默认 2。先 `_isolate_cpu`（`CUDA_VISIBLE_DEVICES=""` + `torch.set_num_threads`）再 import runner，保证 torch 首次 import 时看不到 GPU。
- 末行 JSON `{status, producer, segment_count, message}`；completed / skipped / reclaimed 退出码 0，其余 `status="error"`、退出码 1。
- `query` 只读 `temporal.jsonl` 的分段，不碰 torch。无策略覆盖参数：换策略只能改 YAML。

### OfflineJobService：串行队列 + 每作业一个子进程

单例 `offline_job_service`（`offline/instance.py`），admin 路由直接持有。

- **执行**：`SerialTaskQueue("offline", maxsize=QUEUE_SIZE)`，一次一个作业；子进程跑 `python -m …offline.cli run … --threads 2`，解析 stdout 末行 JSON。不在进程内调 runner：`torch.set_num_threads` 作用于整个进程，且无法中途 kill。
- **子进程环境**：`os.environ` 副本 + `CUDA_VISIBLE_DEVICES=""` + `PYTHONIOENCODING=utf-8`，`cwd` = 仓库根；降优先级 Windows 用 `BELOW_NORMAL_PRIORITY_CLASS`、POSIX 用 `nice -n 15` 命令前缀（不用 `preexec_fn`）；输出写临时文件而非管道。
- **常量**：`THREADS=2`、`QUEUE_SIZE=20`、`JOB_TIMEOUT_S=1800`、`HISTORY=200`、`POLL_S=1`。
- **`submit(run: RunIdentity)`**：`require_offline` 失败 → 400；服务未启动 → 409；该 run 就是当前注册 CQ 的 run（输入还在写）→ 409；同一 run 已有在途作业 → 返回在途那个（按 `RunIdentity` 去重）；队满 → 409。
- **状态** `queued / running / completed / skipped / reclaimed / failed / cancelled`：退出码 0 且 JSON status ∈ {completed, skipped, reclaimed} 才透传，否则 failed（message 先取 JSON，再取 stderr 尾 2000 字符）。`_watch` 只管超时 kill。
- **取消 / 停机**：`cancel(run)` 对排队中的直接 cancelled、运行中的 kill；起子进程与 kill 在同一把锁下。`stop()` 先 kill 运行中的，排队中的在队列排空时逐个记 cancelled。
- 状态只在内存，重启丢失（结果已落盘）。

### CLEAN 离线策略（`offline/impl/clean.py`）

特征工程是模块级纯函数，四个模型类共用基类 `_CleanTorchSegmenter`：`segment` 出逐帧 softmax `[T,C]` → argmax → 跳过背景类 idle、同类合并、丢弃短于 `min_duration_s` 的段 → `TemporalSegment`（`meta.model_version`），并缓存 `LabelProbs`。未配 `model_path` → `ValueError`（不做规则降级）；权重缺失 → `FileNotFoundError`。bbox 归一化优先用帧级 `frame_width/height`，缺失才回退构造参数（默认 640×480）。

- **默认 `CleanNodepGRUSegmenter`**：
  - `preprocess`：`resample_by_ts(frames, model_input_fps, strict=True)` → 226 维特征 `ama-v3-concat23-nodep-226d`（v2 113 维 ⊕ v3 113 维 scope 器械轴坐标系；读入层丢弃废弃类 `scope_distal_end / short_brush / long_brush`）。
  - 推理：每帧取以它为末帧的 16 帧因果窗口（开头用首帧补齐），单向 3 层 GRU(hidden=128) + Linear，分批 1024 → softmax `[T,6]`；类别 `idle / water_injection / flush / long_brush_insert / long_brush_withdraw / short_brush_cleaning`。
  - 只读 `.pt` 的 `model_state`（`weights_only=True`、`strict=True`）；hidden / 层数 / window 写死在类里。构造时 `model_input_fps` 须 > 0，`confidence_override` 缺省 None（用真实置信度）、须在 [0,1]。
  - **配错不报错**：window 与特征版本无运行时校验；`model_input_fps` / `confidence_override` 须与训练口径一致（当前取值的训练口径待核验）；特征按整段计算、非因果，不能原样搬到在线。
- **三个整段双向模型**（备选）：`CleanMSTCNBiLSTMSegmenter`（113 维）、`CleanASFormerSegmenter`（+business_priors，121 维）、`CleanBiGRUSegmenter`（+window_stats+business_priors，249 维），多态只在 `preprocess`。`torch.load(weights_only=False)`（checkpoint 含 numpy normalizer）+ `strict=True`，校验 checkpoint 的 `feature_version` / `feature_names`，不一致即 `ValueError`。YAML 只需给 `model_path` / `min_duration_s`；类别表是另一套 6 类（含 `air_injection`，无 `water_injection`）。
- 权重命名 `clean-offline-<模型>.pt`（`gru-nodep` / `bigru` / `asformer` / `mstcn`），放 `${CLEANSIGHT_MODEL_PATH:./app/data}`，训练在独立 `offline-model` 仓。在线进程从不实例化 `offline` 块的类，类路径错、权重缺失只让离线作业 failed。

## 压力观测落点

日志契约见 [DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)。`StageAwareDispatcher` 经 `PressureReporter` 打 `[PRESSURE] resource=stage_queue`，`_stage_drops`（deque 满淘汰）与 `_stage_rejects`（`submit` 返 False）并入同一行；`VisualizationWorker` 打 `[VIZ_THROUGHPUT]`。

## Stage 配置：schema 与校验口

`config/inference_config.yaml`：每个 stage 三段 `detectors[]` / `rules[]` / `offline`，主键 = step_id 字符串，`alias` = 可读名；顶层另有 `batch_size`。当前 stage 内容与阈值见 [BUSINESS_DETECTION_STANDARDS.md](BUSINESS_DETECTION_STANDARDS.md)，fps 类配置的分层见 [SERVICE_CONFIG.md](SERVICE_CONFIG.md)（YAML 里唯一的 fps 是 `model_input_fps` 模型契约）。

- `offline` 形状 `{class, params}`：缺省或 `{}` = 不可跑离线；非空时 `class` 必填可导入。
- 校验口都无兜底：在线 `resolve_stage`（`/api/start` 400），离线 `require_offline`（作业提交 400 / CLI 退出码 1）。

## naming.py：运行时注册表

由 `InferenceService.start()` 经 `StageFactory` 初始化，单测未初始化时惰性回落 YAML：

- `_TASK_METRIC_MAP: {流名 → AlarmMetric}`，供 `signals_10s`。只收 `realtime: true` 规则订阅的流，且要求 `AlarmMetric(流名.upper())` 存在，否则 warning 后跳过——当前只有 `bubble` 进表，CLEAN 的 `clean_large` / `clean_small` 虽属 `realtime: true` 规则也被跳过。
- `_STAGE_ALIAS_MAP: {stage 主键 → alias}`，写入告警 `stage` 字段与可视化叠字；未命中回退主键。

## 代码来源

- `app/services/inference/__init__.py`、`{config,stage_factory,resample}.py`
- `app/services/inference/online/{service,instance,naming,render,types}.py`
- `app/services/inference/online/detection/{detector,dispatcher,infer_proxy,stage_worker,service}.py`
- `app/services/inference/online/temporal/{operator,actor,alarm_sink}.py`
- `app/services/inference/online/visualization/{visualization_worker,visualizer}.py`
- `app/services/inference/online/{detection,temporal}/impl/{bubble,bending,clean}.py`
- `app/services/inference/offline/{segmenter,runner,cli,service,instance}.py`、`offline/impl/clean.py`
- `app/services/run_control/{__init__,service}.py`（`lifespan` / `shutdown`）
- `app/types/{detection,temporal,run,alarm}.py`、`app/storage/inference/`
- `config/inference_config.yaml`
- `tests/test_infer_proxy.py`、`tests/test_inference_stage_routing.py`、`tests/test_writeback_handle_fence.py`、`tests/test_inference_resample.py`、`tests/test_offline_pipeline.py`、`tests/test_offline_job_service.py`、`tests/doubles.py`、`integration_tests/test_offline_job_subprocess.py`
