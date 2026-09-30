> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 数据流

实时流从输入到展示、落盘、告警的主路径，以及 run 结束后的离线分析链。运行键为 int `task_id`；盘上产物按 run
分目录，身份是 `RunIdentity(task_id, step_id, run_id)`（`app/types/run.py`），CQ 构造时绑定 `cq.run`。

## 端到端路径

```text
RTSP
  -> StreamService / FFmpegDecoder（自持读循环线程；ffmpeg 输出规范化 CFR raw_fps 流）
  -> ClientQueues.ca_raw（raw HLS 纯缓冲）
  -> ClientQueues.ca_ready（SPSC deque，每 N 帧留 1，N = inference_decimation）
  -- L1 --> StageAwareDispatcher（唯一提交者，捕获 CQ 句柄）-> RemoteInferProxy.submit -> 推理子进程
             -> collector 组装 FrameDetection（带 cq 句柄）
             DetectionService._write_back_results（单入口，判 cq.is_active()）三写：
               ├─ push_detection -> _slide_window        （-> L3）
               ├─ set_latest_detection                   （-> Viz 快照）
               └─ append_ca_detections -> ca_detections  （落盘缓冲；降级帧不入）
  -- L3/L4 --> ClientTemporalActor（2Hz tick）-> Operator.analyze()/judge()
                 -> set_latest_temporal（前端事件）
                 -> alarm_sink.persist_alarms（过闸 + 入 alarm 队列上报）
  -- Viz --> VisualizationWorker（独立线程，轮询快照渲染）
               ├─ append_ca_processed -> ca_processed（processed HLS 纯缓冲）
               └─ set_latest_rendered -> _latest_rendered 快照（WS /ai/video 由前端轮询，非后端 push）
  -- PULL --> recording.SegmentSweeper 每 1s collect_from(cq)
                 ├─ ca_raw / ca_processed -> "recording" 队列            -> {run}/hls/
                 └─ drain_ca_detections   -> "recording-detections" 队列 -> {run}/inference/detections.jsonl
```

`{run}` = `{storage_base_dir}/{task_id}/{step_id}/{run_id}/`，由 `start_run` 里的 `runs.allocate` 在构造 CQ 前建好；
盘上布局见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)。

## 输入与解码

每个 `task_id` 一个 `FFmpegDecoder`。ffmpeg 以 `scale=W:H,fps=raw_fps` + `-vsync` 输出 CFR rawvideo；读循环线程
逐帧取墙钟 `time.time()` 作 `Frame.timestamp`，写 `ca_raw`、经 `append_ca_ready_with_throttle`（整数降采样 + 背压）写
`ca_ready`，并更新 `latest_raw_frame/timestamp`（健康监控、可视化用）。

## 推理与时序（L1→L4）：帧捕获 ts 是全链路对齐锚点

- **单提交者**：`StageAwareDispatcher` 从各 run 的 `ca_ready` 取帧、按 stage 捕获 CQ 句柄入 `_stage_queues`，
  轮转提交给 `RemoteInferProxy`（布尔背压，拒收即帧留 deque）。GPU 前向在 spawn 子进程串行执行。
- **cq 句柄不过进程边界**：子进程只收纯数据（`_infer_models`）；collector 线程从 `_Pending` 把 cq 与分辨率盖回 `FrameDetection`。
- **ts 锚点**：`Frame.timestamp` 穿透到 `detector.infer_batch(frames, timestamps)` 并写入各流
  `DetectorOutput.timestamp`，同帧多流 ts 精确相等，`FrameDetection.by_source` 据此对齐；detector 不得自造时间戳。
- **写回单入口、零 IO**：`_write_back_results` 取走 `frame.cq` 并置 None，判 `cq.is_active()`——迟到写落到
  DRAINING/CLOSED 的旧 CQ 即丢弃、计 `frame_drop_total{reason="stale_run"}`，不串台。检测结果落盘由 recording 来拉，
  inference 不依赖 recording。
- **降级帧不落盘**：任一源 `success=False` 的帧照写帧窗与快照，但不入 `ca_detections`——落盘格式不带 `success`，
  空框会被离线当成「没检出」。
- `ClientTemporalActor` per-run 以 `tick_interval=0.5s` 读 `_slide_window` 跑 operators，产前端事件与告警。

细节见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)；压力观测点 `[PRESSURE]` / `[VIZ_THROUGHPUT]` 见
[DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)。

## 落盘走 PULL：recording 拉，两条队列互不阻塞

CQ 的 `ca_raw` / `ca_processed` / `ca_detections` 都是纯缓冲，不触发落盘。`SegmentSweeper` 是纯节拍器，每 1s 对每个
活跃 CQ 调一次 `RecordingService.collect_from`，由服务决定取什么、按什么顺序取。两条 `SerialTaskQueue` 各一个消费线程：

- `recording`：HLS 段写入 `{run}/hls/`，格式归 `app.storage.hls`；
- `recording-detections`：`inference.append_detections(cq.run, frames)` 追加 `detections.jsonl`，格式归 `app.storage.inference`。

分两条是因为段写含 ffmpeg 转码（单段 0.26–3 s），检测结果排在后面会被一起背压丢弃且无声。产物只写进 `cq.run`
指向的目录，旧 run 的迟到写落回旧目录。

**残帧 flush 只在 `stop_run` 发生**：`stop_run` 依次 `stop_workflow`（收结算告警）→ `recording.flush_residual(cq)`
（切完不足一段的残帧，并排空 `ca_detections`）→ 注销 CQ（`cq.close()`）。触发方是 `/api/terminate`、`/api/start`
重启、health_monitor，以及进程停机（`run_control.lifespan` 逐个 stop_run，见
[ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)）。详见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)。

告警上报在 `app/services/alarm/`（入队 + HTTP 上报与重试，见 [SERVICE_ALARM.md](SERVICE_ALARM.md)）；过闸去重与
模式归属在 `inference/online/temporal/alarm_sink`。存储 TTL 清理由 `app/daemons/cleanup/` 负责。

## 在线与离线只经盘上产物相连

在线由 recording 写 `detections.jsonl`；离线读它、写 `temporal.jsonl` / `label_probs.npz`。两段代码互不 import，
共用的 `config.py` / `stage_factory.py` / `resample.py` 在 `app/services/inference/` 顶层。

```text
{run}/inference/detections.jsonl
  -> OfflineRunner.run(OfflineRunSpec{task_id, step_id, run_id?})
       require_offline(step_id) → 解析并锁定一个 run → read_detections → segmenter → 校验
       → ① label_probs.npz（可选旁路）→ ② temporal.jsonl（丢旧 TemporalSegment、保留 TemporalEvent、原子写回）
```

- **独立 OS 进程**，不进 uvicorn：CLI 在 torch import 前置 `CUDA_VISIBLE_DEVICES=""` 并限线程。入口有两个：手动
  `python -m app.services.inference.offline.cli run|query --task-id T --step-id S [--run-id R]`；admin 页
  `POST /admin-f3m8/offline/jobs` → `OfflineJobService`（串行、起 CLI 子进程、超时 kill）。
- **写方唯一**：`detections.jsonl` 只由 recording 写；`temporal.jsonl` / `label_probs.npz` 只由离线 Runner 写。
  同一 run 跨进程并发跑离线无互斥。
- **复用在线契约**：输入 `FrameDetection`，输出 `TemporalSegment` / `LabelProbs`；ts 为帧捕获墙钟浮点秒（毫秒规则的具名例外）。
- **读口**：`POST /ai/temporal`（分段 → 媒体毫秒）、`POST /lab-f3m8/label-probs`（逐帧概率），都经 `resolve_timeline`
  锁定同一 run 的媒体轴；`GET /lab-f3m8/tasks` 的 `offline_steps` 由 `inference.query_has_offline_results` 判定。
- **未实现**：run 结束后自动触发、离线判合规 / 告警、结果入库。

Runner 各步的失败语义（skipped / reclaimed / 校验规则）见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)「`OfflineRunner`」。

## 代码来源

- `app/services/stream/decoder.py`、`app/services/client/queues.py`
- `app/services/inference/online/detection/{dispatcher,infer_proxy,stage_worker,service}.py`
- `app/services/inference/online/temporal/{actor,alarm_sink}.py`、`app/services/inference/online/visualization/visualization_worker.py`
- `app/services/inference/offline/{runner,cli,service}.py`、`app/services/inference/config.py`
- `app/services/recording/{service,sweep_worker}.py`、`app/services/run_control/service.py`（`start_run` / `stop_run` / `shutdown`）
- `app/storage/{hls,inference}/`、`app/storage/runs.py`、`app/types/{detection,temporal,run}.py`
