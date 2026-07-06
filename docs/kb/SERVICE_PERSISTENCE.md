> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Persistence Service

持久化服务负责 HLS 视频段落盘、metadata 与告警上报，是**无状态落库层**：慢 IO 与实时路径解耦，过闸/去重编排不在此（见下）。

## PersistenceManager

`PersistenceManager` 启动四件套：`hls_queue`（HLSWorkerPool，默认 2 worker）、`alarm_queue`（AlarmWorkerPool，默认 1 worker）、`_segment_sweeper`（HLSSegmentSweeper 守护线程）、`_cleanup_worker`（StorageCleanupWorker 守护线程）。公开方法：

- `start()` / `stop(timeout)`：`stop` 按 sweeper→hls→alarm→cleanup 顺序停（先停 sweeper，杜绝新段入队）。
- `persist_hls_segment(task_id, step_id, segment_type, frames) → bool`：入 `hls_queue`。
- `persist_alarm(alarm_info: Dict) → bool`：入 `alarm_queue`（**纯入队**，无过闸/去重）。
- `start_run(cq)`：`hls_pool.purge_step_dir(task_id, step_id)` 清空旧 HLS step 目录（无 owner，纯 rmtree），与 inference 的 `FeatureStore.open_fresh` 对称。
- `flush_residual_segments(cq)`：拆除时排空 CQ raw/processed 缓冲，按 `ca_segment_len` 切块入队。
- `release_task_locks(task_id)`：`hls_pool.release_dir_locks(task_id)` 回收该 task 的目录锁。

## PULL 模型：CQ 纯缓冲，sweeper 主动拉

HLS 分段落盘为 **PULL**：CQ 的 `ca_raw`/`ca_processed` 是纯缓冲、不触发落盘。`HLSSegmentSweeper`（`snapshot_fn=client_manager.snapshot`，`interval_seconds=1.0`）周期遍历快照，对每个 CQ 调 `take_raw_segment()` / `take_processed_segment()` 拉整段（攒满 `ca_segment_len` 才弹），再经 `persist_hls_segment` 入 `hls_queue`。旧的 CQ 主动 PUSH 已退役。

## 告警落库归属（无状态）

`persist_alarm(alarm_info)` 只做 DB/HTTP 落库入队。**过闸去重（5s 冷却）+ mode 归属 + 别名烧录在 inference 侧**：`ClientQueues.append_alarm_record_with_gate` 管去重，`inference/temporal/alarm_sink.persist_alarms` 管编排（实时/结算），持久化只读 `alarm_info` 里已定好的字段落库。`AlarmWorker` 用 `GuardedExecutor` 调 `AlarmPersistenceStrategy.report_alarm()` HTTP POST 到 `settings.alarm_report_url`；停机后 drain 队列尽量不丢。

## HLS 持久化与 fMP4

`HLSPersistenceStrategy.persist_segment()` 按 `{storage_base_dir}/{task_id}/{step_id}` 分 raw/processed 写：

- `{track}_segment_{ts_us}.mp4`：cv2 写 mp4v → ffmpeg 转 HLS-ready fMP4 fragment。
- `{track}_playlist.m3u8`（含 `#EXTINF`）、`init.mp4`（step 级共享，写一次）、`metadata.json`、`.hls_timescale`。
- **keypoints JSON 死写已删**（不再写 `keypoints_*.json`）。

`_dir_locks: {target_dir → Lock}`：transcode + playlist append + metadata 更新在目录锁内原子完成（相邻段需读 playlist 算累计时间，防 tfdt 碰撞）；`release_dir_locks(task_id)` 在拆除时删该 task 前缀的所有锁。

**processed 段按实测 fps 编码**：`_effective_fps(frames, fallback)` 由帧时间戳跨度 `(N-1)/(ts_last-ts_first)` 求得（回落 `settings.inference_fps`），同一 fps 用于 cv2.VideoWriter 与 EXTINF（详见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)）。

## 存储根单一真源

存储根统一读 `settings.storage_base_dir`（`PersistenceConfig.storage_base_dir` 转发）。persistence 不反向摸 inference，inference 也不摸 persistence 私有 `db_dir`——三方（persistence/FeatureStore/traceback）经 settings 对齐同一根。

## 清理

`StorageCleanupWorker`（`storage.enable_cleanup` 控制，默认开）周期清理已完成且超保留天数的任务目录。

## 代码来源

- `app/services/persistence/manager.py`
- `app/services/persistence/workers/{hls_worker,alarm_worker,cleanup_worker,segment_sweeper}.py`
- `app/services/persistence/strategies/{hls_strategy,alarm_strategy}.py`
- `app/services/persistence/models.py`、`config.py`
- `app/services/inference/temporal/alarm_sink.py`（过闸编排归属）
- `app/settings.py`（storage_base_dir 单一真源）
- `config/persistence_config.yaml`
