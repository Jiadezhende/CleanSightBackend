> 更新时间：2026-07-11
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Inference Service

推理服务负责 stage 路由、模型推理（L1）、特征落盘（L2）、时序分析 + 判定（L3/L4）、可视化与结算告警。代码已按**处理流程分包**：`detection/ feature/ temporal/ visualization/ workflows/ offline/` + 顶层管件（`manager.py`/`instance.py`/`models.py`/`naming.py`/`stage_factory.py`/`config.py`）。

## 包结构（子包 = 分层）

| 子包/文件 | 层 | 关键类 | 职责 |
|-----------|----|--------|------|
| `detection/detector.py` | L1 | `Detector`（含 `YOLODetector`） | 无状态 GPU 推理，帧→`FrameDetections` + 可视化数据；多 run 共享 |
| `detection/dispatcher.py` | L1 | `StageAwareDispatcher` | 轮询各 run `pop_ca_ready()`，按 stage 分组，**捕获 CQ 句柄**进 `DetectionTask` |
| `detection/pool.py` | L1 | `MultiModelWorkerPool` | 按 detector batch 推理（可选 CUDA stream），返回 `FrameInference`（携 `cq`） |
| `detection/service.py` | L1 | `ModelWorkerService` | 管 dispatcher + worker pool；`_write_back_results` 单入口写回 |
| `feature/store.py` | L2 | `FeatureStore` / `FactLedger` | per-`(task_id,step_id)` 落 `features.jsonl`；owner fence |
| `temporal/operator.py` | L3/L4 | `Operator` | analyze+judge 合并，持 `_sm`，`subscribes` 显式，`window_seconds` 感受野 |
| `temporal/actor.py` | L3/L4 | `ClientTemporalActor` | per-run ~1Hz tick，跑 operators，烧 stage 别名，收结算告警 |
| `temporal/alarm_sink.py` | L4 | `persist_alarms()` | 过闸（CQ gate）+ 落库（persistence），实时/结算统一入口 |
| `visualization/worker.py` | Viz | `VisualizationWorker` | 读快照渲染，写 `ca_processed` + `_latest_rendered` |
| `visualization/visualizer.py` | Viz | `FixedVisualizer` | 按 `RenderSpec` 固定渲染 |
| `workflows/` | 接入点 | bubble/bending/clean/mock | 具体 Detector + Operator 子类 |
| `offline/` | 离线 | —（仅 `__init__` 占位） | **待实现**：Segmenter/Runner 读 FeatureStore→写 FactLedger |

## InferenceManager（生命周期编排）

`manager.py` 单例 `inference_manager`（`instance.py` 惰性构造，配置 fail-fast）。公开方法：

- `start()` / `stop()`：注册 dispatcher/pool + 初始化 naming 表；`stop()` 两阶段（先 `signal_stop` 全 actor，再 join 收 settlement + flush FeatureStore）。
- `start_workflow(cq)`：`FeatureStore.open_fresh`、按 stage 实例化 operators、建并起 `ClientTemporalActor`。入参是 RunController **已注册**（`client_manager.set`）的 CQ——本方法不再碰注册表（set/remove 均归 RunController，与 `stop_run` 对称）。
- `stop_workflow(cq) → List[Alarm]`：pop actor、finalize 收结算告警、关 feature 分区，返回 settlement 告警（交 RunController 落库）。
- `resolve_stage(step_id) → str`：**恒等路由**——`str(step_id)` 命中 stage 配置键则返回，否则告警回落 `MOCK`。stage 是 CQ 不可变身份的一部分（构造时定死）。
- `set_stream_windows` / `status`。

`start_workflow` / `stop_workflow` 的互斥由 RunController 的 `lock_for(task_id)` 承接，本类不自持 per-client 锁；`_actors: {task_id → ClientTemporalActor}`。

## L1 写回：单入口 + 句柄化 + 状态门

`ModelWorkerService._write_back_results(List[FrameInference])` 是 L1 唯一写回口。每条结果先判 `res.cq.is_active()`（DRAINING/CLOSED 丢弃 → `stale_run` 计数），ACTIVE 才三写：

1. `cq.push_detection(FrameFeature(ts=res.timestamp, by_source=res.detections))` → 帧级 `_slide_window`（`Deque[FrameFeature]`，写回口一次物化整帧多流；供 L3，异步缓冲解速差 30fps↔1Hz）。
2. `cq.set_latest_inference(feature)` → 原子快照（同一 `FrameFeature`，供 Viz；无 `cq`，不成自引用环。Viz 的 stage 取自 `cq.stage`）。
3. `feature_store.append(task_id, step_id, res, owner=cq)` → L2 落盘（供离线）。

全程携 CQ 句柄（`res.cq`，dispatcher pop 时捕获），**无 `client_id` 反查**。

## Detector / Operator 两粒度框架

- **Detector**（流源，分组粒度，无状态共享）：`name`（= 产出流名 = slide_window key）、`infer_batch(frames, timestamps)`（**唯一推理入口**，无单帧 `infer()`）、`prepare_visualization_data`。`timestamps` 是帧捕获真值锚点（源自 `Frame.timestamp`，pool 从 `req.timestamp` 穿入），须原样写回 `FrameDetections.timestamp`，令每帧 `FrameDetections.timestamp == FrameInference.timestamp`——写回口据此物化帧级 `FrameFeature` 对齐多流（供 L3），detector 不得自造时间戳。YOLO 类继承 `YOLODetector` 复用惰性加载/batch/CUDA 异常转换。
- **Operator**（流算子，规则粒度，per-run 独立）：`name`、`subscribes`（**显式、必填**输入流名列表，缺则 fail-fast）、`window_seconds`；`analyze(windows)` 推进 `self._sm`、`judge() → (overlay_texts, alarms)`、`finalize() → List[Alarm]`（结算，默认空）。analyze+judge **合并**进 Operator（不再有独立 TemporalAnalyzer/Judge，不做 EventFact 跨对象传递）。

## L3/L4：ClientTemporalActor（~1Hz）

per-run daemon 线程，`stop_event.wait(tick_interval)` 节奏。每 tick 取一次帧窗 `windows = cq.get_slide_window()`（`List[FrameFeature]`，多流已在写回口对齐），对每个 operator：`op.analyze(windows)`（算子内自行 `_clip` + 按 `subscribes` 投影）→ `op.judge()` 收 (events, alarms)；汇总后 `cq.set_latest_temporal(events)`，`alarm_sink.persist_alarms(alarms, cq, mode=REALTIME)`。**per-operator 隔离**（单算子异常不断整 tick）。stage 别名在告警离开推理前烧进 `alarm.stage`。停机两段：`signal_stop()` → `finalize_and_stop() → List[Alarm]`（收各 operator `finalize()` 结算告警）。

## 告警落库：alarm_sink.persist_alarms

`persist_alarms(alarms, *, cq, mode, log_each=False)`：从 CQ 取 `task_id/step_id/source_ip`；逐条设 `alarm.mode`，`cq.append_alarm_record_with_gate(alarm, mode)` 过 5s 冷却闸 + 入环形日志，再 `persistence_manager.persist_alarm(...)` 落库。**过闸编排归推理产出域，persistence 只做无状态落库**。别名由 Actor 先烧好，sink 直读 `alarm.stage`。

## online / offline 分离

实时链（L1→`_slide_window`→L3 tick 1Hz）与离线链（`FeatureStore.load` → OfflineSegmenter → FactLedger）**彻底分离**：实时不落 FactLedger，Actor 不 load 事实。当前 `offline/` 仅占位，消费端（Segmenter/Runner）**待实现**（见 `docs/update/20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md`）。

## Stage 配置与当前阶段

`config/inference_config.yaml`，每 stage 三段 `detectors[]` / `rules[]` / `offline{}`，主键 = step_id 字符串，`alias` = 可读名：

- `"1"` / alias `LEAK`：detectors `bubble` + `bending`；rules `bubble_leak`（realtime）、`bending_check`（settlement）。
- `"2"` / alias `CLEAN`：detectors `clean_large` + `clean_small`；`rules: []`（无 Operator/Actor，仅检测框可视化）。
- `MOCK`：未知 step fallback + taskless 默认，纯透传（`mock_passthrough` 恒不触发）。

跨模块共享参数（`raw_fps`/`inference_fps`/`ca_maxlen`/`ca_segment_len`）已上浮 `app/settings.py` 单一真源；本文件只留 `batch_size` 等推理自有参数。

## naming.py 运行时注册表

由 `InferenceManager.start()` 经 `StageFactory` 初始化（单测缺失时惰性回落 YAML）：

- `_TASK_METRIC_MAP: {detector_name → AlarmMetric}`（仅 `realtime:true` 规则的流），供 signals_10s。
- `_STAGE_ALIAS_MAP: {stage_key → alias}`（如 `"1"→"LEAK"`），落告警 `stage` 字段 + 可视化叠字。

## 代码来源

- `app/services/inference/manager.py`、`instance.py`、`stage_factory.py`、`naming.py`、`models.py`、`config.py`
- `app/services/inference/detection/{detector,dispatcher,pool,service}.py`
- `app/services/inference/feature/store.py`
- `app/services/inference/temporal/{operator,actor,alarm_sink}.py`
- `app/services/inference/visualization/{worker,visualizer,pool}.py`
- `app/services/inference/workflows/{bubble,bending,clean,mock}.py`
- `config/inference_config.yaml`
