> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Persistence Service

持久化服务负责 HLS 视频段、keypoints JSON、metadata 和告警上报。

## PersistenceManager

`PersistenceManager` 创建两类队列：

- `hls_queue`
- `alarm_queue`

并启动：

- `HLSWorkerPool`
- `AlarmWorkerPool`
- 可选 `StorageCleanupWorker`

配置来自 `config/persistence_config.yaml` 和推理配置中的 fps 参数。

## 队列解耦与慢 IO 分离

持久化服务也是队列解耦设计。实时推理、时序分析和可视化路径不会直接执行磁盘写入、ffmpeg 转码或 HTTP 上报，而是把任务提交到有界队列：

- `hls_queue`：承接 raw/processed 视频段写入、fMP4 转码、playlist、metadata、keypoints JSON。
- `alarm_queue`：承接告警 HTTP 上报。

这样做的主要收益：

- 慢 IO 与实时路径分离，避免 HLS 转码、磁盘抖动、外部 HTTP 延迟阻塞解码和推理。
- HLS 与告警两类任务独立排队、独立 worker 消费，互不拖慢。
- 队列满时入队失败并记录 `hls_queue_full` 或 `alarm_queue_full` 指标，形成明确背压信号。
- worker 内部用 `GuardedExecutor` 处理重试，失败不会直接击穿上游实时流程。

当前默认配置中 HLS worker 为 2 个，告警 worker 为 1 个。AlarmWorker 在停止信号后会 drain 队列剩余告警，尽量避免告警丢失；HLSWorker 当前按 stop_event 退出，不显式 drain，因此任务结束前的残余视频段主要依赖 `InferenceManager.remove_client()` 主动 flush 入队并由服务生命周期继续消费。

## HLS 持久化

`persist_hls_segment()` 将 `HLSPersistenceTask` 入队。

`HLSPersistenceStrategy.persist_segment()` 按 `{base_dir}/{task_id}/{step_id}` 创建目标目录，并分 raw/processed 写入。

raw 写入：

- `raw_segment_{ts_us}.mp4`
- `raw_playlist.m3u8`
- `metadata.json`

processed 写入：

- `processed_segment_{ts_us}.mp4`
- `keypoints_{ts_us}.json`
- `processed_playlist.m3u8`
- `metadata.json`

## fMP4 转码

cv2 先写 mp4v 段，随后通过 ffmpeg 转为 HLS-ready fMP4 fragment。`init.mp4` 是 step 级共享。playlist append 和 metadata 更新在目录锁内完成。

## 告警持久化

`persist_alarm()` 将 `AlarmPersistenceTask` 入队。

`AlarmWorker` 使用 `GuardedExecutor` 调用 `AlarmPersistenceStrategy.report_alarm()`，通过 HTTP POST 上报到 `settings.alarm_report_url`。

告警 payload 包含：

- `task_id`
- `step_id`
- `step_name`
- `alarm_type`
- `alarm_level`
- `alarm_message`
- `alarm_time`
- 可选 `detection_result`

## 清理

`StorageCleanupWorker` 是否启用由 `storage.enable_cleanup` 控制，默认配置为启用，清理已完成且超过保留天数的任务目录。

## 代码来源

- `app/services/persistence/manager.py`
- `app/services/persistence/workers/hls_worker.py`
- `app/services/persistence/workers/alarm_worker.py`
- `app/services/persistence/workers/cleanup_worker.py`
- `app/services/persistence/strategies/hls_strategy.py`
- `app/services/persistence/strategies/alarm_strategy.py`
- `config/persistence_config.yaml`
