> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 数据流

本文件描述实时流从输入到展示、落盘和告警的主路径。运行键全链路为 int `task_id`。

## 端到端路径

```text
RTSP (仅 RTSP)
  -> StreamService / FFmpegDecoder（decoder 自持读循环；ffmpeg 输出规范化 CFR raw_fps 流）
  -> ClientQueues.ca_raw（raw HLS 纯缓冲）
  -> ClientQueues.ca_ready（SPSC deque，Bresenham 均匀抽帧 inference_fps/raw_fps）
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

`StreamService` 为每个 `task_id` 创建一个 `FFmpegDecoder`（仅 RTSP）。ffmpeg 用 `scale=W:H,fps=raw_fps` + `-vsync` 输出**规范化 CFR raw_fps** rawvideo 流；decoder **自持读循环线程**读帧（合并双平台单一阻塞读路径），`Frame.timestamp` 取读帧时的墙钟到达时刻（`time.time()`）。主要输出：`ca_raw`（raw HLS 缓冲）、`ca_ready`（待推理，`append_ca_ready_with_throttle` Bresenham 相位累加抽帧 + 背压）、`latest_raw_frame/timestamp`（健康监控/可视化）。

## 推理与时序（L1→L4）

`StageAwareDispatcher` 轮询各 run `ca_ready`，按 stage 分组，pop 时**捕获 CQ 句柄**进 `DetectionTask`。每 stage 独立推理线程消费 batch，返回 `FrameInference`（携 `cq`）。写回由 `ModelWorkerService._write_back_results` 单入口完成，先判 `cq.is_active()`（迟到写落到 DRAINING/CLOSED 旧 CQ 被丢弃、不串台），再三写 `_slide_window` / `_latest_inference` / `FeatureStore`。`ClientTemporalActor` per-run ~1Hz 读 `_slide_window` 跑 operators，产前端事件 + 告警。详见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)。

## 可视化与前端

`VisualizationWorker` 独立线程轮询各 run，读最新推理快照 / 最新原始帧 / 最新时序事件，渲染后写 `ca_processed`（processed HLS 缓冲）与 `_latest_rendered`（供 `/ai/video` WS 前端轮询）。观测点：`[INFER_PRESSURE]`（推理背压）、`[VIZ_THROUGHPUT]`（可视化吞吐）。

## 落盘与告警（PULL 模型）

HLS 分段落盘为 **PULL**：CQ 的 `ca_raw`/`ca_processed` 是纯缓冲，不触发落盘；persistence 的 `HLSSegmentSweeper` 周期 `take_raw_segment()`/`take_processed_segment()` 主动拉整段。持久化两条队列：HLS queue（raw/processed mp4、playlist、metadata；keypoints 死写已删）、Alarm queue（HTTP 上报）。告警过闸编排在 `inference/temporal/alarm_sink`，persistence 只做无状态落库。

## online / offline 分离

实时链（L1→`_slide_window`→L3 1Hz）与离线链（`FeatureStore.load` → OfflineSegmenter → FactLedger）彻底分离：实时不落 FactLedger。离线消费端**待实现**，当前仅有 L2 特征落盘半程。

## 代码来源

- `app/services/stream/service.py`、`app/services/stream/decoder.py`
- `app/services/client/queues.py`
- `app/services/inference/detection/{dispatcher,service}.py`
- `app/services/inference/temporal/{actor,alarm_sink}.py`
- `app/services/inference/visualization/worker.py`
- `app/services/inference/feature/store.py`
- `app/services/persistence/manager.py`、`app/services/persistence/workers/segment_sweeper.py`
