> 更新时间：2026-07-21
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
| `temporal/operator.py` | L3/L4 | `Operator` / `TemporalOperator` | analyze+judge 合并，持 `_sm`，`subscribes` 显式，`window_seconds` 感受野；`TemporalOperator` 子基类另持 torch 时序模型（惰性 `torch.jit.load`）做动作识别 |
| `temporal/actor.py` | L3/L4 | `ClientTemporalActor` | per-run ~1Hz tick，跑 operators，烧 stage 别名，收结算告警 |
| `temporal/alarm_sink.py` | L4 | `persist_alarms()` | 过闸（CQ gate）+ 落库（persistence），实时/结算统一入口 |
| `visualization/worker.py` | Viz | `VisualizationWorker` | 读快照渲染，写 `ca_processed` + `_latest_rendered` |
| `visualization/visualizer.py` | Viz | `FixedVisualizer` | 按 `RenderSpec` 固定渲染 |
| `workflows/` | 接入点 | bubble/bending/clean/mock | 具体 Detector + Operator 子类 |
| `offline/` | 离线 | `OfflineSegmenter`/`OfflineRunner` + `segmenters/{clean,mock}` + `cli` | 独立进程读 `FeatureStore.load`→策略 `preprocess`/`segment`→`OfflineRunner` 校验幂等写 `FactLedger`（详见「离线 segmenter 内部」与 online/offline 分离） |

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
3. `feature_store.append(task_id, step_id, feature, owner=cq)` → L2 落盘同一份帧级 `FrameFeature`（供离线；两端货币一致）。

全程携 CQ 句柄（`res.cq`，dispatcher pop 时捕获），**无 `client_id` 反查**。

## Detector / Operator 两粒度框架

- **Detector**（流源，分组粒度，无状态共享）：`name`（= 产出流名 = slide_window key）、`infer_batch(frames, timestamps)`（**唯一推理入口**，无单帧 `infer()`）、`prepare_visualization_data`。`timestamps` 是帧捕获真值锚点（源自 `Frame.timestamp`，pool 从 `req.timestamp` 穿入），须原样写回 `FrameDetections.timestamp`，令每帧 `FrameDetections.timestamp == FrameInference.timestamp`——写回口据此物化帧级 `FrameFeature` 对齐多流（供 L3），detector 不得自造时间戳。YOLO 类继承 `YOLODetector` 复用惰性加载/batch/CUDA 异常转换。
- **Operator**（流算子，规则粒度，per-run 独立）：`name`、`subscribes`（**显式、必填**输入流名列表，缺则 fail-fast）、`window_seconds`；`analyze(windows: List[FrameFeature])` 推进 `self._sm`、`judge() → (overlay_texts, alarms)`、`finalize() → List[Alarm]`（结算，默认空）。analyze+judge **合并**进 Operator（不再有独立 TemporalAnalyzer/Judge，不做 EventFact 跨对象传递）。`windows` 是帧级 `FrameFeature` 快照（多流已在写回口对齐进 `by_source`），算子内 `_clip` 到自身感受野，单订阅用 `primary_window` 投影自身流。
- **TemporalOperator**（`Operator` 子基类，供动作识别时序模型）：多带 `model_path`/`objects`/`actions` 三参，惰性 `torch.jit.load`（双检锁、缺文件 `FileNotFoundError`、失败 `_load_failed` 锁存），`infer(features)` 前向出 logits。子类 `CleanOperator`（`workflows/clean.py`）在 `analyze` 内把订阅流窗口 `_adapt_to_features` 成 `(T, num_objects×6)` 张量（每物体 `(count,cx,cy,w,h,area)`，异常帧留全零行保持时间轴不缺帧）后 `infer`，取末步 argmax 存 `_sm['latest_action']`；`judge` 仅出 overlay 文案、当前不产告警。这是 CLEAN stage 的**在线**时序算子（YAML `clean_monitor`，`gru-final.pt`，`window_seconds=10`），与离线 CLEAN segmenter 是两条独立链路。

## L3/L4：ClientTemporalActor（~1Hz）

per-run daemon 线程，`stop_event.wait(tick_interval)` 节奏。每 tick 取一次帧窗 `windows = cq.get_slide_window()`（`List[FrameFeature]`，多流已在写回口对齐），对每个 operator：`op.analyze(windows)`（算子内自行 `_clip` + 按 `subscribes` 投影）→ `op.judge()` 收 (events, alarms)；汇总后 `cq.set_latest_temporal(events)`，`alarm_sink.persist_alarms(alarms, cq, mode=REALTIME)`。**per-operator 隔离**（单算子异常不断整 tick）。stage 别名在告警离开推理前烧进 `alarm.stage`。停机两段：`signal_stop()` → `finalize_and_stop() → List[Alarm]`（收各 operator `finalize()` 结算告警）。

## 告警落库：alarm_sink.persist_alarms

`persist_alarms(alarms, *, cq, mode, log_each=False)`：从 CQ 取 `task_id/step_id/source_ip`；逐条设 `alarm.mode`，`cq.append_alarm_record_with_gate(alarm, mode)` 过 5s 冷却闸 + 入环形日志，再 `persistence_manager.persist_alarm(...)` 落库。**过闸编排归推理产出域，persistence 只做无状态落库**。别名由 Actor 先烧好，sink 直读 `alarm.stage`。

## online / offline 分离

实时链（L1→`_slide_window`→L3 tick 1Hz）与离线链（`FeatureStore.load` → OfflineSegmenter → FactLedger）**彻底分离**：实时不落 FactLedger，Actor 不 load 事实。两链共用一套货币 **帧级 `FrameFeature`**（`ts + {source: FrameDetections}`）：写回口 `append(feature)` 落盘，离线 `load(task_id, step_id) → List[FrameFeature]`（一次到位、多流已对齐）喂 `OfflineSegmenter.preprocess(frames)`。`offline/` 已接入（独立进程 CLI `python -m app.services.inference.offline.cli run`，见 `runner.py`/`segmenters/`）。

## 离线 segmenter 内部（`offline/`）

一策略 = 一个 `OfflineSegmenter` 子类（自包含单文件，实现不散落框架层）；`preprocess(frames)` 抽象、无默认特征工程，`segment(model_input) → List[SegmentFact]`。`OfflineRunner.run(spec)`：`load_many` → `preprocess`/`segment` → `_validate_and_stamp`（每条 `SegmentFact.source==策略 name`、`start<=end`、有限数、`0<=conf<=1`）→ `FactLedger.replace_segments`（按 producer 幂等替换）→ 若策略 `debug_result()` 非空补落 `offline_inference_result.json`。入口 `python -m app.services.inference.offline.cli run --task-id --step-id`（独立进程，torch import 前先置 `CUDA_VISIBLE_DEVICES=""` + 限核，CPU-only），`query` 子命令只读 FactLedger。

- **`segmenters/mock.py` `BrushRulesSegmenter`**：纯规则、不依赖 torch/权重，任一订阅 source 有检测框即判该帧 active、连续帧并段。承担 MOCK stage 端到端 smoke + 非法配置兜底——是**离线的真兜底而非脚手架**。
- **`segmenters/clean.py`**：CLEAN 三种时序模型集中一文件——`CleanMSTCNBiLSTMSegmenter`（MS-TCN+BiLSTM）、`CleanASFormerSegmenter`、`CleanBiGRUSegmenter`，`CleanSegmenter` 别名默认指向前者。特征工程是**模块级纯函数**（`build_base_features` 出基础 v2 特征 + `add_business_priors`/`add_centered_window_stats`）；多态只在各 segmenter override `preprocess`（基础 `super().preprocess()` 后叠加自己的 recipe），无集中 `feature_method` 路由分支。三模型特征维不同：base v2=113、+business_priors=121、+window_stats+business_priors=249。
  - **权重严格加载**：`torch.load(..., weights_only=False)`（checkpoint 含 numpy normalizer，PyTorch≥2.6 默认拒反序列化；带旧版 `TypeError` fallback）、`load_state_dict(strict=True)`，并校验 checkpoint `feature_version`/`feature_names` 与后端输入一致——不一致即 `ValueError` 硬失败，杜绝"看似跑通实际没加载"。
  - **无权重硬失败**：未配 `model_path` 直接 `ValueError`，**不做规则降级**（旧 `_RuleDecoder`/`fallback_to_rules` 已删）；本地无权重回环走 MOCK stage。特征矩阵有 `NaN/inf→0` 兜底（拼接后 + normalizer 后各一次）。
  - 训练/导出权重在独立 `offline-model` 仓，后端只加载 checkpoint 推理；`.pt` 权重不入后端仓。当前权重为 baseline，仅验证工程链路闭环；自动任务结束触发离线 Runner/Judge/复算告警/入库仍未实现。

## 推理链路压力与吞吐观测

两条正交的**仅压力时打印**诊断日志（平稳静默，均 try/except 包裹绝不影响热路径），与既有 `[BACKPRESSURE]`（入口/录制队列）并存：

- `[INFER_PRESSURE]`（backlog）：把此前无计数的两个静默丢帧点暴露出来——dispatcher `_stage_queues[stage]` 满淘汰（`_stage_drops`）、`ClientQueues.ca_processed` 满淘汰（`frames_dropped_processed`，与 `frames_dropped_raw` 对称、`clear()` 不重置）。dispatcher 约 10s 评估一次，有新增丢帧（delta>0）或队深 ≥ `maxlen*_pressure_queue_ratio` 才打印；日志同给累计值与自上次的 delta。
- `[VIZ_THROUGHPUT]`（throughput）：VizWorker 量真实成帧 fps / 空转占比（`_stat_stale`，去重命中即上游供帧慢）/ 单帧渲染耗时，据"渲染峰值是否逼近 tick 预算"自动标 `render-bound` / `supply-bound`——定位 processed HLS 回放快放（成帧 fps≪编码 fps）根因在哪一级。顺带删了 `_render` 里外层 `frame.copy()`（render 内部已 copy），省一次整帧 memcpy/帧。

## Stage 配置与当前阶段

`config/inference_config.yaml`，每 stage 三段 `detectors[]` / `rules[]` / `offline{}`，主键 = step_id 字符串，`alias` = 可读名：

- `"1"` / alias `LEAK`：detectors `bubble` + `bending`；rules `bubble_leak`（realtime）、`bending_check`（settlement）；`offline: {}`。
- `"2"` / alias `CLEAN`：detectors `clean_large` + `clean_small`；rule `clean_monitor`（`CleanOperator`，订阅两流，`gru-final.pt` 在线动作识别，realtime）。`offline: {}`（生产默认不启用；启用时改 `offline.class` 指向 `segmenters/clean.py` 某模型）。
  > **旧结论已作废**：早前 CLEAN `rules: []`、仅检测框可视化——现已加了在线时序算子 `clean_monitor`，会建 Operator/Actor。
- `MOCK`：未知 step fallback + taskless 默认，在线纯透传（`mock_passthrough` 恒不触发）；`offline` 段启用 `BrushRulesSegmenter` 作**唯一**端到端离线样例（生产 stage 的 offline 保持 `{}`）。

跨模块共享参数（`raw_fps`/`inference_fps`/`ca_maxlen`/`ca_segment_len`）已上浮 `app/settings.py` 单一真源；本文件只留 `batch_size` 等推理自有参数。

## naming.py 运行时注册表

由 `InferenceManager.start()` 经 `StageFactory` 初始化（单测缺失时惰性回落 YAML）：

- `_TASK_METRIC_MAP: {detector_name → AlarmMetric}`（仅 `realtime:true` 规则的流），供 signals_10s。
- `_STAGE_ALIAS_MAP: {stage_key → alias}`（如 `"1"→"LEAK"`），落告警 `stage` 字段 + 可视化叠字。

## 代码来源

- `app/services/inference/manager.py`、`instance.py`、`stage_factory.py`、`naming.py`、`models.py`、`config.py`
- `app/services/inference/detection/{detector,dispatcher,pool,service}.py`
- `app/services/inference/feature/store.py`
- `app/services/inference/temporal/{operator,actor,alarm_sink}.py`（`operator.py` 含 `Operator` + `TemporalOperator`）
- `app/services/inference/visualization/{worker,visualizer,pool}.py`（`worker.py` 含 `[VIZ_THROUGHPUT]`）
- `app/services/inference/workflows/{bubble,bending,clean,mock}.py`（`clean.py` 含在线 `CleanOperator`）
- `app/services/inference/offline/{segmenter,runner,cli}.py`、`offline/segmenters/{clean,mock}.py`
- `config/inference_config.yaml`
