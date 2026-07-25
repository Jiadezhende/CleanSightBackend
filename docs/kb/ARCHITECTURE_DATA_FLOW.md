> 更新时间：2026-07-25
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 数据流

本文件描述实时流从输入到展示、落盘和告警的主路径。运行键全链路为 int `task_id`。

## 端到端路径

```text
RTSP (仅 RTSP)
  -> StreamService / FFmpegDecoder（decoder 自持读循环；ffmpeg 输出规范化 CFR raw_fps 流）
  -> ClientQueues.ca_raw（raw HLS 纯缓冲）
  -> ClientQueues.ca_ready（SPSC deque，整数降采样每 N 帧留 1，N=inference_decimation）
  -- L1 --> StageAwareDispatcher（捕获 CQ 句柄）-> MultiModelWorkerPool -> FrameInference
             ModelWorkerService._write_back_results（单入口，判 cq.is_active()）三写：
               ├─ push_detection -> _slide_window       （-> L3，异步缓冲解速差）
               ├─ set_latest_inference                  （-> Viz 原子快照）
               └─ FeatureStore.append -> features.jsonl  （L2 落盘，-> 离线）
  -- L3/L4 --> ClientTemporalActor（~1Hz tick）-> Operator.analyze()/judge()
                 -> set_latest_temporal（前端事件）
                 -> alarm_sink.persist_alarms（过闸 + 落库）
  -- Viz --> VisualizationWorker（独立线程，轮询快照渲染）
               ├─ append_ca_processed -> ca_processed（processed HLS 纯缓冲）
               └─ set_latest_rendered -> _latest_rendered 快照
                    ├─ PULL: HLSSegmentSweeper 周期 take_*_segment() -> HLS 分段落盘
                    └─ WS /ai/video 前端 ~10ms 轮询快照（非后端 push）
```

## 输入与解码

`StreamService` 为每个 `task_id` 创建一个 `FFmpegDecoder`（仅 RTSP）。ffmpeg 用 `scale=W:H,fps=raw_fps` + `-vsync` 输出**规范化 CFR raw_fps** rawvideo 流；decoder **自持读循环线程**读帧（合并双平台单一阻塞读路径），`Frame.timestamp` 取读帧时的墙钟到达时刻（`time.time()`）。主要输出：`ca_raw`（raw HLS 缓冲）、`ca_ready`（待推理，`append_ca_ready_with_throttle` 整数降采样每 N 帧留 1 + 背压）、`latest_raw_frame/timestamp`（健康监控/可视化）。

## 推理与时序（L1→L4）

`StageAwareDispatcher` 轮询各 run `ca_ready`，按 stage 分组，pop 时**捕获 CQ 句柄**进 `DetectionTask`。每 stage 独立推理线程消费 batch，返回 `FrameInference`（携 `cq`）。**帧捕获 ts 是真值锚点**：`Frame.timestamp` 由 pool（`req.timestamp`）一路穿透到 `detector.infer_batch(frames, timestamps)`，写入各帧 `FrameDetections.timestamp`，令同帧多流 ts 精确相等——下游 `_zip_by_ts` 按此内连对齐，detector 不得自造时间戳（否则交集为空漏帧）。写回由 `ModelWorkerService._write_back_results` 单入口完成，先判 `cq.is_active()`（迟到写落到 DRAINING/CLOSED 旧 CQ 被丢弃、不串台），再三写 `_slide_window` / `_latest_inference` / `FeatureStore`。`ClientTemporalActor` per-run ~1Hz 读 `_slide_window` 跑 operators，产前端事件 + 告警。详见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)。

## 可视化与前端

`VisualizationWorker` 独立线程轮询各 run，读最新推理快照 / 最新原始帧 / 最新时序事件，渲染后写 `ca_processed`（processed HLS 缓冲）与 `_latest_rendered`（供 `/ai/video` WS 前端轮询）。观测点：`[INFER_PRESSURE]`（推理背压）、`[VIZ_THROUGHPUT]`（可视化吞吐）。

## 落盘与告警（PULL 模型）

HLS 分段落盘为 **PULL**：CQ 的 `ca_raw`/`ca_processed` 是纯缓冲，不触发落盘；persistence 的 `HLSSegmentSweeper` 周期 `take_raw_segment()`/`take_processed_segment()` 主动拉整段。持久化两条队列：HLS queue（raw/processed mp4、playlist、metadata）、Alarm queue（HTTP 上报）。告警过闸编排在 `inference/temporal/alarm_sink`，persistence 只做无状态落库。

## online / offline 分离

实时链（L1→`_slide_window`→L3 1Hz）与离线链（`FeatureStore.load` → `OfflineSegmenter` → `FactLedger`）彻底分离：实时不落 FactLedger。离线消费端是**单一 Runner 路径**：

```text
{base}/{task}/{step}/features.jsonl（在线 FeatureStore.append 常开）
  -> OfflineRunner.run(OfflineRunSpec{task_id, step_id[, strategy]})
       FeatureStore.load(task_id, step_id)           读回 List[FrameFeature]（utf-8-sig 容忍 Windows BOM）
       -> config.resolve_stage(step_id)              数字命中即恒等，未知回退 MOCK 并 WARN
       -> stage_factory.create_offline_segmenter()   非空 offline 段即启用，缺字段 fail-fast
       -> OfflineSegmenter.preprocess(streams)       预处理预留层（clean 私有把订阅流按 ts 跨 source 拍平成 List[FrameDetections] → 62 维 ModelInput）
       -> OfflineSegmenter.segment(model_input)       逐帧分类 → 归并 SegmentFact
       -> 全量校验（source==name / start<=end / 有限数 / 0<=conf<=1，任一非法整批失败）
       -> FactLedger.replace_segments(task_id, step_id, producer, facts)   幂等替换（持锁 + 原子 os.replace，按 producer 过滤）
       -> 可选 segmenter.debug_result() -> offline_inference_result.json    逐帧调试产物
```

要点：

- **独立 OS 进程**，不进 uvicorn；入口在 torch import 前置 `CUDA_VISIBLE_DEVICES=""` + 限线程，与在线链路**零代码/进程耦合、资源不抢占**。手动入口 `python -m app.services.inference.offline.cli run|query --task-id N --step-id M [--strategy PATH]`。
- **复用现有数据契约**：输入吃 `FrameDetections`/`Detection`（`app.domain.detection`），输出吐 `SegmentFact`（`app.services.inference.models`）；只保留 `ModelInput`（62 维数值矩阵，clean 策略私有）一个离线专有表示，无独立中间数据壳。
- **单一 Runner 路径**：框架仅 `offline/{segmenter.py(基类),runner.py,cli.py}` + `segmenters/{clean,mock}.py`，无并行分派层。新增真实时序模型 = 加一个自包含 `segmenters/<stage>.py` 子类 + YAML `offline.class` 一行（clean.py 已含 MS-TCN/ASFormer/BiGRU 系列 torch 策略基类）。
- **存储键 vs 配置 key 正交**：存储读写始终用原数字 `step_id`；`resolve_stage` 仅决定用哪个 stage 的 offline 配置（未配 → MOCK.offline 兜底、仍读写 `{task}/{step}/` 分区）。
- **在线仍不写 FactLedger**（实时不落事实）；离线是唯一 `facts.jsonl` 写方。
- 后续（未实现）：自动调度、离线 Judge（`SegmentFact` → 合规判断/告警）、结果入库——当前只做链路收敛 + baseline 工程闭环，不判合规、不告警。

## 代码来源

- `app/services/stream/service.py`、`app/services/stream/decoder.py`
- `app/services/client/queues.py`
- `app/services/inference/detection/{dispatcher,service}.py`
- `app/services/inference/temporal/{actor,alarm_sink}.py`
- `app/services/inference/visualization/worker.py`
- `app/services/inference/feature/store.py`（`FeatureStore.load` / `FactLedger.replace_segments`）
- `app/services/inference/offline/{runner,segmenter,cli}.py`、`app/services/inference/offline/segmenters/{clean,mock}.py`
- `app/services/inference/config.py`（`resolve_stage`）、`app/services/inference/stage_factory.py`（`create_offline_segmenter`）
- `app/services/persistence/manager.py`、`app/services/persistence/workers/segment_sweeper.py`
