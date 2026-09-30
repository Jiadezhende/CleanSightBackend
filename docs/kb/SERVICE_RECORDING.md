> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Recording Service（录制落盘）

`app/services/recording/` 是在线落盘的唯一生产写侧，产出两类东西：HLS 段、帧检测结果 `detections.jsonl`，分别写进 `cq.run` 指向的 `{task_id}/{step_id}/{run_id}/hls/` 与 `inference/`。本服务只管**何时拉、按什么顺序写**：

```text
app/services/recording/   编排：何时拉、顺序、失败怎么办
app/storage/hls/          格式：段文件名、m3u8、fMP4 字节、sidecar、编解码
app/storage/inference/    格式：detections.jsonl 的 record 形状与追加
```

数据层不持锁，「同一 `(run, track)` 的段写串行」「同一 run 的 detections 追加串行」都由本服务的队列保证（数据层见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)、[DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)）。换代隔离不归本服务：一次 run 一个目录，由 `run_control` 在锁内经 `runs.allocate` 分配。

## 对外成员

| 成员 | 调用方 | 语义 |
|------|--------|------|
| `start()` / `stop(timeout)` | `recording.lifespan()` | 起停两条队列与 sweeper |
| `collect_from(cq)` | SegmentSweeper（唯一） | 取走该 CQ 此刻该落盘的一切 |
| `submit_segment(cq, track, frames) -> bool` | 内部 + 单测 | 打包成段任务入 `"recording"` 队列；False = 这段不会被写 |
| `submit_detections(cq, frames) -> bool` | 内部 + 单测 | 打包成任务入 `"recording-detections"` 队列；False = 这批不会被写 |
| `flush_residual(cq, until_ts=None)` | `RunControlService.stop_run`、`collect_from` | 把不足一段的残帧切段入队，末尾把 `ca_detections` 全排空一并交出 |
| `request_residual_flush(cq, fence_ts)` | HealthMonitorWorker（断流时） | **只登记**一次残帧 flush，由 sweeper 下一 tick 执行 |

- **run 身份由 `cq.run` 带**：打包时取出 `RunIdentity` 放进 `_SegmentJob(run, track, frames)` / `_DetectionJob(run, frames)`，任务不持 cq。`cq.run is None` 的 CQ 定位不到目录：`submit_*` 返回 False，`flush_residual` / `request_residual_flush` 跳过，`collect_from` 不取帧。
- **入队成功 ≠ 写成功**：落盘在队列线程上异步发生。
- **`until_ts` 只作用于段**：`None`（拆除期）全排空；给值（断流期）只切栅栏前的帧，重连后的新帧留给 sweeper 照常拉整段。detections 两种情况都全排空。拆除期调用须早于 `cq.close()`，由 `stop_run` 保证。

## 生命周期：recording 在 inference 外层，停机残段也能落盘

lifespan 嵌在 inference 外层、与 alarm 同档。停机时最内层的 `run_control` 先逐个 `stop_run`，`flush_residual` 把各 run 的残段与剩余检测结果交给仍活着的队列；等 inference 停完，recording 的 `finally` 再停（完整停机顺序见 [ARCHITECTURE_OVERVIEW.md](ARCHITECTURE_OVERVIEW.md)）。

- `start()` 建两条 `SerialTaskQueue` 并起 `SegmentSweeper`。队列在 `start()` 里建而不在 `__init__`：`SerialTaskQueue` 是一次性的，`stop()` 后不能再 `start()`。
- `stop()` **先停 sweeper，再停两条队列排空**。反过来会在队列停机后继续拉，提交被拒而帧已从 CQ 弹出，是真丢。两条队列之间没有先后要求。队列排空超时（10 s）只记 warning，剩余任务随进程退出丢弃。

## 并发模型：无锁，顺序靠单消费队列，隔离靠 run 目录

```text
同一 run 内的顺序    SerialTaskQueue 提交序 = 执行序（两条队列各一个消费线程）
跨 run 的隔离        盘上一 run 一目录（旧一代迟到写进自己的目录，新一代读不到）
```

三条不变式，破了都不报错、只是数据静默损坏：

1. **两条队列都不能加 worker**：同一 run 内相邻段的 tfdt 按执行顺序累计，并发会碰撞。`config/recording_config.yaml` 因此没有 `workers` 项。
2. **运行期 CQ 的 drain 者只能是 sweeper 一个线程**，入口是 `collect_from`。断流走 `request_residual_flush` 登记，由 sweeper 那一轮执行。
3. **`_pending_flush: {(task_id, step_id) → (cq, fence_ts)}` 只做单次 dict 操作**：它被三个线程碰——health_monitor 写、sweeper 取（`_take_pending_flush`）、拆除路径回收（`flush_residual(until_ts=None)`）。免锁靠 `__setitem__` / `get` / `pop` 各自是一次原子 C 调用，不逐元素迭代。

run 目录已被 TTL 回收时，写口抛 `FileNotFoundError`（写者不建 run 目录），这批丢弃。

**失败不重试**：`_write` / `_write_detections` 各只调一次 `hls.insert_segment` / `inference.append_detections`，异常由 `SerialTaskQueue._execute` 记 error 吞掉。`insert_segment` 最后才登记清单条目，重试可能写出重复条目、毁掉整个 run 的回放；`append_detections` 纯追加，重试会写出重复帧；会抛的失败（ffmpeg 缺失、盘满、权限、run 已回收）都不是瞬时的。丢一段 ≈ 丢 10 s 录像，比写坏整个 run 便宜（判据见 [DESIGN_FAULT_TOLERANCE.md](DESIGN_FAULT_TOLERANCE.md)）。

## Sweeper 是纯节拍器

`sweep_worker.SegmentSweeper`（包内私有，线程名 `RecordingSweeper`）每隔 `sweep_interval_seconds` 遍历 `client_service.snapshot()`，对每个 CQ 调 `service.collect_from(cq)`；拉什么、按什么顺序、挂起请求怎么办全在 `collect_from`。它传整个 `cq` 而不是 `task_id`：帧归属的 run 必须在取帧那一刻捕获，晚一步去注册表取，可能已是新一代 CQ。`sweep_worker` 不 import 任何单例，`clients` 与 `service` 都是注入的。

## collect_from 的四步顺序定死

```text
① 拉 raw 整段        take_raw_segment 循环
② 拉 processed 整段  take_processed_segment 循环
③ 断流残帧           有挂起请求才做：_take_pending_flush → flush_residual(until_ts=fence)，做完直接 return
④ 拉 detections      submit_detections(cq, cq.drain_ca_detections())
```

- **③ 必须在 ①② 之后**：残段的帧 ts 晚于本轮所有整段。入队序即执行序，每段 tfdt 是执行时读到的累计 EXTINF，顺序一乱，后写的段就在媒体轴上盖掉先写的，不报错，只是画面丢一截。
- **④ 与 ①②③ 无顺序约束**：另一条队列、另一个文件，与媒体轴无关。③ 发生时 detections 已由 `flush_residual` 末尾交出，本轮不再 drain。

## 检测结果走第二条队列

```text
推理写回口 → cq.append_ca_detections(frame)        只入缓冲，写回线程零 IO；降级帧不入
sweeper → collect_from ④ → _DetectionJob(run, frames)
        → "recording-detections" 队列 → inference.append_detections(run, frames)
        → {run}/inference/detections.jsonl
拆除 / 停机：stop_run → flush_residual(cq) 末尾全排空
```

- **两条队列**：段写含 ffmpeg 转码（单段 0.26–3 s），detections 排在它后面会跟着被背压丢，一次十几帧、而且静默。
- **缓冲放在 CQ 上**：与 `ca_processed` 同形，拉模式不产生 inference → recording 的依赖边，也保住「drain 者只有 sweeper」。容量与丢弃口径见 [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)。
- 每 tick 有多少交多少（15 fps 下约 15 帧一批）。`detections.jsonl` 的唯一写者是本服务，离线 runner 只读（`inference.read_detections(run)`）。

## 断流残帧 flush：让空洞落在段边界上

断流重连不拆 CQ、不清队列。若不处理，断流那刻的半批帧会被重连后的帧补满、拼成横跨 gap 的段；`eff_fps` 由首末帧跨度反推，10 s 画面被写成 30 s EXTINF（3× 慢放），回放 / 导出 / 送标一起中招。`eff_fps≈9.99` 仍在合理带 `[1,60]` 内、不触发退化兜底，全程无报警，每次重连必现。

```text
HealthMonitorWorker._enter_reconnect_mode
    写 _reconnecting_clients[task_id] 之后
    → recording.request_residual_flush(cq, fence_ts=断流前最后一帧 ts)     只登记
HealthMonitorWorker._handle_reconnecting_client（判定重连成功时）
    → recording.request_residual_flush(cq, 同一个 fence_ts)                再登记一次
sweeper 下一 tick → collect_from ③ → flush_residual(cq, until_ts=fence)
    → drain_ca_raw(until_ts) / drain_ca_processed(until_ts)，只弹队首满足栅栏的连续前缀
```

- **首次登记在进入重连时，不能等成功**：成功的判据就是「已经来了新帧」，那时残批里已混进重连后的帧。
- **重连成功时用同一栅栏再登记一次**：processed 轨由 viz worker 从推理结果渲染，ts 落后 raw 一个推理管线延迟，断流前的 processed 帧可能在首次 flush 之后才入队。栅栏是时间戳、重连后的帧都大于它，重复登记只会捞走迟到的断流前帧。
- **不能就地 drain**：两次 drain 各自受 CQ 锁保护、帧不重不漏，但 `submit_segment` 在锁外，谁先入队由调度决定；入队序一乱 tfdt 就乱，队列只保证执行序 = 入队序，挡不住这个。
- **挂起请求带 cq 做身份核对**：`_take_pending_flush`（包内私有，唯一消费者 `collect_from`）取走时对象身份不匹配 = 属于同 `(task, step)` 的上一代 CQ，直接丢弃；拆除路径只 pop 属于本 cq 的条目（`stop_run` 持 `lock_for`，期间不会有新一代登记同键）。

## 配置

`config/recording_config.yaml` → `RecordingConfig`（扁平 dataclass；文件不存在或解析失败用默认值、记日志；出现未知字段直接抛）：

| 项 | 默认 | 语义 |
|----|------|------|
| `queue_size` | 100 | 每条队列各自的上限；满了 `submit_*` 返回 False 并 warning。不给无界选项 |
| `sweep_interval_seconds` | 1.0 | 扫描间隔，远小于段周期（≈10 s）与 CQ 缓冲容量（≈30 s） |

不在这里配的：`workers`（不变式 1）、段时长与帧率（段长由 `settings.ca_segment_seconds` 按帧数触发，段时长由写侧从帧 ts 反推）。

## 单例与引用面

单例 `recording_service` 只许被 `run_control/service.py`、`routers/*`、本包 `lifespan()` 引用，外加具名例外 `app/daemons/health_monitor/worker.py`（`_resolve_deps()` 函数体内取；门禁 `test_singleton_reference_surface`）。`__init__.py` 只有 docstring + `lifespan()`，不 re-export，免得把 `app.storage.hls` 与 client → numpy 链摊给每个 import 者。

## 代码来源

- `app/services/recording/{__init__,instance,service,sweep_worker,config}.py`、`config/recording_config.yaml`、`app/services/utils/task_queue.py`
- `app/storage/hls/`、`app/storage/inference/`、`app/services/client/queues.py`
- `app/services/run_control/service.py`（`flush_residual` 调用点）、`app/daemons/health_monitor/worker.py`（`request_residual_flush` 调用点）
- `tests/test_recording_service.py`、`tests/test_client_detections_buffer.py`、`tests/test_task_queue.py`、`tests/test_storage_hls.py`
