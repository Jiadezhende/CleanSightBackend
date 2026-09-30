> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 数据流

本文件描述实时流从输入到展示、落盘和告警的主路径，以及 run 结束后的离线分析链。运行键全链路为 int `task_id`；盘上产物按 run 分目录，身份是 `RunIdentity(task_id, step_id, run_id)`（`app/types/run.py`），CQ 构造时绑定 `cq.run`。

## 端到端路径

```text
RTSP (仅 RTSP)
  -> StreamService / FFmpegDecoder（decoder 自持读循环；ffmpeg 输出规范化 CFR raw_fps 流）
  -> ClientQueues.ca_raw（raw HLS 纯缓冲）
  -> ClientQueues.ca_ready（SPSC deque，整数降采样每 N 帧留 1，N=inference_decimation）
  -- L1 --> StageAwareDispatcher（唯一提交者，捕获 CQ 句柄）-> RemoteInferProxy.submit -> 推理子进程
             -> collector 组装 FrameDetection（带 cq 句柄）
             DetectionService._write_back_results（单入口，取走 frame.cq，判 cq.is_active()）三写：
               ├─ push_detection -> _slide_window        （-> L3，异步缓冲解速差）
               ├─ set_latest_detection                   （-> Viz 原子快照）
               └─ append_ca_detections -> ca_detections  （落盘缓冲；降级帧不入）
  -- L3/L4 --> ClientTemporalActor（2Hz tick）-> Operator.analyze()/judge()
                 -> set_latest_temporal（前端事件）
                 -> alarm_sink.persist_alarms（过闸 + 入 alarm 队列上报）
  -- Viz --> VisualizationWorker（独立线程，轮询快照渲染）
               ├─ append_ca_processed -> ca_processed（processed HLS 纯缓冲）
               └─ set_latest_rendered -> _latest_rendered 快照
                    └─ WS /ai/video 前端 ~10ms 轮询快照（非后端 push）
  -- PULL --> recording.SegmentSweeper 每 1s collect_from(cq)
                 ├─ ca_raw / ca_processed -> hls 队列        -> {run}/hls/
                 └─ drain_ca_detections   -> detections 队列 -> {run}/inference/detections.jsonl
```

`{run}` = `{storage_base_dir}/{task_id}/{step_id}/{run_id}/`，由 `runs.allocate` 在 `/api/start` 时建（早于 CQ 构造），盘上布局见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)。

## 输入与解码

`StreamService` 为每个 `task_id` 创建一个 `FFmpegDecoder`（仅 RTSP）。ffmpeg 用 `scale=W:H,fps=raw_fps` + `-vsync` 输出**规范化 CFR raw_fps** rawvideo 流；decoder **自持读循环线程**读帧（合并双平台单一阻塞读路径），`Frame.timestamp` 取读帧时的墙钟到达时刻（`time.time()`）。主要输出：`ca_raw`（raw HLS 缓冲）、`ca_ready`（待推理，`append_ca_ready_with_throttle` 整数降采样每 N 帧留 1 + 背压）、`latest_raw_frame/timestamp`（健康监控/可视化）。

## 推理与时序（L1→L4）

`StageAwareDispatcher` 是**唯一提交者**：`_fetch_and_dispatch_round` pop 各 run `ca_ready`、按 stage **捕获 CQ 句柄**进 `_stage_queues` deque，再 `_drain_and_submit` peek-commit 轮转排空 → `RemoteInferProxy.submit`（布尔背压，拒收即帧留 deque）。GPU 前向在独立 **spawn 子进程**串行执行，collector 守护线程据 req_id 组装 `FrameDetection`（**cq 句柄不过进程边界**，切口在纯数据 `_infer_models`；collector 从 `_Pending` 把 cq 与帧分辨率盖进 `FrameDetection`）。**帧捕获 ts 是真值锚点**：`Frame.timestamp` 一路穿透到 `detector.infer_batch(frames, timestamps)`，写入各流 `DetectorOutput.timestamp`，令同帧多流 ts 精确相等，`FrameDetection.by_source` 据此对齐，detector 不得自造时间戳。

写回由 `DetectionService._write_back_results` 单入口完成：先取走 `frame.cq` 并置 None（留存下来的帧一律不带 cq），判 `cq.is_active()`（迟到写落到 DRAINING/CLOSED 旧 CQ 被丢弃、计 `frame_drop_total{reason="stale_run"}`、不串台），再把同一对象三写帧窗 / 最新快照 / 落盘缓冲。**写回线程零 IO**：不碰存储，检测结果落盘由 recording 来拉（拉模式不产生 inference → recording 依赖边）。任一源 `success=False` 的降级帧照写帧窗与快照，**不入落盘缓冲**——落盘格式不带 `success`，空框会被离线当成「没检出」。`ClientTemporalActor` per-run 以 `tick_interval=0.5s`（2Hz）读 `_slide_window` 跑 operators，产前端事件 + 告警。详见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)。

## 可视化与前端

`VisualizationWorker` 独立线程轮询各 run，读最新检测快照 / 最新原始帧 / 最新时序事件，渲染后写 `ca_processed`（processed HLS 缓冲）与 `_latest_rendered`（供 `/ai/video` WS 前端轮询）。观测点：`[PRESSURE]`（队列积压/拒收）、`[VIZ_THROUGHPUT]`（可视化吞吐），统一见 [DESIGN_OBSERVABILITY.md](DESIGN_OBSERVABILITY.md)。

## 落盘与告警（PULL 模型）

落盘为 **PULL**：CQ 的 `ca_raw` / `ca_processed` / `ca_detections` 都是纯缓冲，不触发落盘；周期拉取者是 **recording 的 `SegmentSweeper`**（纯节拍器，每 1s 对每个活跃 CQ 调一次 `RecordingService.collect_from`，由服务决定取什么、按什么顺序取）。recording 持**两条** `SerialTaskQueue`，各自单消费线程、互不阻塞：

- `recording`：HLS 段，写进 `cq.run` 的 `{run}/hls/`，格式归 `app.storage.hls`；
- `recording-detections`：检测结果，`inference.append_detections(cq.run, frames)` 追加 `{run}/inference/detections.jsonl`，格式归 `app.storage.inference`。

分两条是因为段写含 ffmpeg 转码（单段 0.26–3 s），检测结果排在它后面会被一起背压丢、且静默。拆除时 `RunControlService.stop_run` → `stop_workflow` → `recording.flush_residual(cq)`（切完残段，末尾把 `ca_detections` 全排空交出）→ `cq.close()`。产物只写进各自 run 目录，旧一代迟到写入落回旧 run，换代隔离在盘上成立。残帧 flush 只发生在 per-run 拆除（`stop_run`，由 `/api/terminate`、`/api/start` 重启与 health_monitor 触发）：**进程停机不经 `stop_run`**，cq 里不足一段的残帧（≤ 约 10 s 录像）与最后约 1 s 的检测结果不落盘（已知缺口）。详见 [SERVICE_RECORDING.md](SERVICE_RECORDING.md)。

告警上报在 `app/services/alarm/`（无状态入队 + HTTP 上报与重试，见 [SERVICE_ALARM.md](SERVICE_ALARM.md)）；过闸去重 / 模式归属编排在 `inference/online/temporal/alarm_sink`。存储 TTL 清理是独立 daemon `app/daemons/cleanup/`，见 [ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)。

## online / offline 分离

实时链（L1 → `_slide_window` → L3 2Hz）与离线链只经盘上产物相连：在线由 recording 写 `detections.jsonl`，离线读它、写 `temporal.jsonl` / `label_probs.npz`。两段代码互不 import（`app/services/inference/__init__.py`），共用的 `config.py` / `stage_factory.py` / `resample.py` 平铺在 `app/services/inference/` 顶层。

离线一次运行只处理**一个锁定的 run**，单一 Runner 路径：

```text
{run}/inference/detections.jsonl（recording 写；降级帧不落）
  -> OfflineRunner.run(OfflineRunSpec{task_id, step_id, run_id?})
       config.require_offline(step_id)          未定义 / offline 空块 → ValidationError（无兜底）
       StageFactory.create_offline_segmenter    producer = 类名
       runs.query(task, step, run_id)           入口解析一次 run 并锁定，之后全程只读写它
                                                点名的 run 不在 → reclaimed；缺省且无可见 run → skipped
       inference.read_detections(run)           空 → skipped（不覆盖旧结果）
       segmenter.preprocess -> segment          算法异常上抛，不写
       _validate（producer==name / start<=end / 有限数 / 0<=conf<=1，任一非法整批失败）+ 排序
       ① label_probs.npz（可选旁路，形状不符 / 写失败只告警）
       ② temporal.jsonl：读回 → 丢全部旧 TemporalSegment、保留 TemporalEvent → 追加本次 → 原子整体写回
          写时 FileNotFoundError 且 run 已不在 → reclaimed（不重建目录）
  -> completed
```

要点：

- **独立 OS 进程**，不进 uvicorn：CLI 入口在 torch import 前置 `CUDA_VISIBLE_DEVICES=""` + 限线程，与在线链路**零代码/进程耦合、资源不抢占**。两个入口：手动 `python -m app.services.inference.offline.cli run|query --task-id T --step-id S [--run-id R]`；admin 页经 `POST /admin-f3m8/offline/jobs` → `OfflineJobService`（串行队列、一次一个作业、起 CLI 子进程、降优先级、超时 kill；提交时该 run 仍是注册 CQ 的 run → 409）。
- **旁路先于事实**：`temporal.jsonl` 落盘即表示本次运行完成，读到新分段时概率必然已是同一次运行的。
- **写方唯一**：`detections.jsonl` 唯一写方是 recording；`temporal.jsonl` / `label_probs.npz` 唯一写方是离线 Runner（在线不写）。同一 run 跨进程并发跑离线无互斥（一期不支持）。
- **复用在线契约**：输入 `FrameDetection`（`app.types.detection`），输出 `TemporalSegment` / 可选 `LabelProbs`（`app.types.temporal`）；ts 为帧捕获墙钟浮点秒（时间量纲 int 毫秒规则的具名例外）。
- **读口**：`POST /ai/temporal`（分段 → 媒体毫秒）、`POST /lab-f3m8/label-probs`（逐帧概率曲线），都经 `resolve_timeline` 锁定同一 run 的媒体轴；`GET /lab-f3m8/tasks` 的 `offline_steps` 由 `inference.query_has_offline_results` 判定。
- 未实现：run 结束后自动触发、离线 Judge（分段 → 合规判断/告警）、结果入库——离线当前不判合规、不告警、不入库。

## 代码来源

- `app/services/stream/service.py`、`app/services/stream/decoder.py`
- `app/services/client/queues.py`（`push_detection` / `set_latest_detection` / `append_ca_detections` / `drain_ca_detections`）
- `app/services/inference/online/detection/{dispatcher,infer_proxy,stage_worker,service}.py`
- `app/services/inference/online/temporal/{actor,alarm_sink}.py`
- `app/services/inference/online/visualization/visualization_worker.py`
- `app/services/inference/offline/{runner,segmenter,cli,service,instance}.py`、`app/services/inference/offline/impl/clean.py`
- `app/services/inference/config.py`（`require_offline`）、`app/services/inference/stage_factory.py`（`create_offline_segmenter`）
- `app/services/recording/{service,sweep_worker}.py`（两条队列的 PULL 落盘写侧）
- `app/storage/hls/`、`app/storage/inference/`、`app/storage/runs.py`
- `app/types/{detection,temporal,run}.py`
- `app/services/alarm/service.py`（告警入队）、`app/daemons/cleanup/worker.py`（TTL）
