> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 数据流

本文件描述实时流从输入到展示、落盘和告警的主路径。

## 端到端路径

```text
RTSP/RTMP
  -> StreamService
  -> FFmpegDecoder
  -> ClientQueues.ca_raw
  -> ClientQueues.ca_ready
  -> StageAwareDispatcher
  -> ModelWorkerService / MultiModelWorkerPool
  -> ClientQueues.slide_window + latest_inference
  -> ClientTemporalActor
  -> PersistenceManager.alarm_queue
  -> VisualizationWorker
  -> ClientQueues.ca_processed + latest_rendered
  -> HLSWorker / WebSocket
```

## 输入与解码

`StreamService` 为每个 `client_id` 创建一个 `FFmpegDecoder`。Decoder 使用 FFmpeg 输出固定尺寸、固定像素格式的 rawvideo，再将帧写入 `ClientQueues`。

主要输出：

- `ca_raw`：原始帧，用于 raw HLS 落盘。
- `ca_ready`：待推理帧，用于推理调度。
- `latest_raw_frame/latest_raw_timestamp`：健康监控和可视化读取。

## 推理与时序

`StageAwareDispatcher` 轮询所有客户端的 `ca_ready`，按 stage 分组入队。每个 stage 有独立推理线程消费 batch。

推理结果双写：

- `slide_window`：按 task/model 存放历史窗口，供 TemporalActor 分析。
- `latest_inference`：原子快照，供 VisualizationWorker 绘制，保证同帧一致。

TemporalActor 每个 client 一个线程，约 1Hz 分析滑动窗口，产出前端事件和告警。

## 可视化与前端

VisualizationWorker 单线程约 15 FPS 轮询所有客户端，读取：

- 最新推理快照
- 最新原始帧
- 最新时序事件

渲染后写入：

- `ca_processed`：用于 processed HLS。
- `latest_rendered`：供 `/ai/video` WebSocket 推送。

## 落盘与告警

`ClientQueues` 在 raw/processed 队列达到 `ca_segment_len` 后将段提交给 `PersistenceManager`。

持久化服务分两条队列：

- HLS queue：写 raw/processed mp4、playlist、metadata、keypoints。
- Alarm queue：通过 HTTP 上报告警。

## 代码来源

- `app/services/stream/service.py`
- `app/services/stream/decoder.py`
- `app/services/client/queues.py`
- `app/services/inference/core/dispatcher.py`
- `app/services/inference/core/service.py`
- `app/services/inference/workers/temporal.py`
- `app/services/inference/workers/visualization.py`
- `app/services/persistence/manager.py`

