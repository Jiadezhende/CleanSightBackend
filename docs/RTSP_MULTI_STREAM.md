# 多路并行数据处理流程

- 每个客户端对应一条独立链路：`FFmpegDecoder`（解码）→ `ClientQueues`（多队列缓存）→ 推理/可视化消费者。
- `FFmpegDecoder` 启动独立的 FFmpeg 子进程，输出 rawvideo 到 stdout；通过 `_process_bytes` 拼帧、转换为 `numpy` 数组并调用 `_standardize_frame`（统一尺寸/通道/类型）。
- 写入两个队列：
	- `ca_raw`：全量原始帧，用于落盘或后续回放（通常不丢弃）。
	- `ca_ready`：供推理使用的降频队列，通过 `append_ca_ready_with_throttle` 控制送入推理的帧率。
- 背压策略：写入前调用 `StreamService.get_pending_count(client_id)` 获取 `ca_ready` 深度，若超过 `PER_STREAM_MAX_PENDING_RATIO`*容量，则丢帧以保护推理端。
- 推理与展示解耦：推理消费 `ca_ready`（较低 FPS，例如 10fps），展示/聚合使用 `ClientQueues` 提供的 `latest_raw_frame` 缓存以较高频率输出（例如 25–30fps），确保画面流畅。
- 容错与监控：`FFmpegDecoder` 支持自动重启和 stderr 日志；在关键点记录接收/写入/丢弃统计以及队列深度，便于定位瓶颈。

该设计目标是确保每一路流拥有独立的解码和本地缓存链路，通过背压与降频保护推理稳定性的同时，使用帧缓存实现高帧率展示体验。
