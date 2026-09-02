> 更新时间：2026-07-25
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

HLS 分段落盘为 **PULL**：CQ 的 `ca_raw`/`ca_processed` 是纯缓冲、不触发落盘。`HLSSegmentSweeper`（`snapshot_fn=client_manager.snapshot`，`interval_seconds=1.0`）周期遍历快照，对每个 CQ 调 `take_raw_segment()` / `take_processed_segment()` 拉整段（攒满 `ca_segment_len` 才弹），再经 `persist_hls_segment` 入 `hls_queue`。

## 告警落库归属（无状态）

`persist_alarm(alarm_info)` 只做 DB/HTTP 落库入队。**过闸去重（5s 冷却）+ mode 归属 + 别名烧录在 inference 侧**：`ClientQueues.append_alarm_record_with_gate` 管去重，`inference/temporal/alarm_sink.persist_alarms` 管编排（实时/结算），持久化只读 `alarm_info` 里已定好的字段落库。`AlarmWorker` 用 `GuardedExecutor` 调 `AlarmPersistenceStrategy.report_alarm()` HTTP POST 到 `settings.alarm_report_url`；停机后 drain 队列尽量不丢。

## HLS 持久化与 fMP4

`HLSPersistenceStrategy.persist_segment()` 按 `{storage_base_dir}/{task_id}/{step_id}` 分 raw/processed 写：

- `{track}_segment_{ts_us}.mp4`：cv2 写 mp4v → ffmpeg 转 HLS-ready fMP4 fragment。
- `{track}_playlist.m3u8`（含 `#EXTINF`）、`{track}_init.mp4`（**按轨各一份**，该轨首段转码时写一次）、`metadata.json`。
- `raw_segment_{ts_us}.idx`：raw 轨逐帧 ts 的 float64 sidecar，供离线帧反查。在 mp4 之前落盘（保证索引不晚于段对 `SegmentFinder` 可见），**写失败只 warning 不阻断本段**——三条视频链路都不读它，不能让辅助索引拖垮主产物。processed 轨不产。

`_dir_locks: {target_dir → Lock}`：transcode + playlist append + metadata 更新在目录锁内原子完成（相邻段需读 playlist 算累计时间，防 tfdt 碰撞）；`release_dir_locks(task_id)` 在拆除时删该 task 前缀的所有锁。

**段按实测 fps 编码（回放对齐墙钟）**：`_persist_processed_segment` / `_persist_raw_segment` 的 `cv2.VideoWriter` 与 `segment_duration`(EXTINF) 同源用静态 `_effective_fps(frames)` = `(N-1)/(ts_last-ts_first)`（**不接收任何上游 fps**，全程从帧 ts 反推）；`span<=0` / 单帧 / 反推值落 `[1,60]` 带外这类**退化段**（无可测速率）回落**本地常量** `_DEGENERATE_FALLBACK_FPS=15.0`（与上游 `raw_fps`/`inference_fps` 无关——退化段本无时序信息，只需给个合理 EXTINF）。逐段各取自身有效帧率，自动吸收速率抖动。
- **为何治本**：processed 链路真实成帧率在窗口间漂移（~11-15fps），若固定按标称 fps 编码则播放 `= 标称/真实` 倍快放（曾观测 20fps 编码、~11fps 成帧 → ~1.8x 快放，段间还忽快忽慢）。这是**时钟/速率失配**，非积压/丢帧，backlog 类指标测不到（帧产得慢、既没进队列也没被丢）。逐段实测 fps 让任何真实帧率都对齐墙钟。
- **raw 段同款**：与 processed 一样走 `_effective_fps(frames)` 从 ts 反推（实测稳定 ~30fps），`raw_fps` 不再作编码常量——统一「消费者读 ts、不设 fps」。
- 三套时间线（EXTINF / tfdt / fragment 媒体时长）仍自洽——EXTINF 仍等于 fragment 媒体时长（详见 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)）。
- 单测：`tests/test_hls_eff_fps.py`（正常反推 / span≤0 / 单帧 / 带外 / 乱序 回退）。

> 速率亏空的上游治理（throttle 整数降采样 + 检测率 15、渲染尾延迟削峰）落在 stream/inference 侧，非本服务。采样旋钮真源是 `inference_decimation`（[app/settings.py](../../app/settings.py)）；本服务 HLS 段编码**不引用任何上游 fps**，从帧 ts 反推 `_effective_fps`（退化段兜底为本地 `_DEGENERATE_FALLBACK_FPS`）。

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
- `app/settings.py`（storage_base_dir 单一真源；HLS 段编码不引用上游 fps）
- `config/persistence_config.yaml`
- `tests/test_hls_eff_fps.py`（processed 段实测 fps 编码）
