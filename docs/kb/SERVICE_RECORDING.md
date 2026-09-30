> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Recording Service（录制落盘）

`app/services/recording/` 是**在线落盘的唯一生产写侧**，负责两类产物：HLS 段，以及帧检测结果（`detections.jsonl`）。它把 CQ 里攒好的东西写进 `cq.run` 指向的 `{task_id}/{step_id}/{run_id}/hls/` 与 `inference/`。本服务只回答**何时拉、按什么顺序写**；格式怎么落盘全在数据层 `app.storage.hls` / `app.storage.inference`（见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md)、[DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)、[ARCHITECTURE_STORAGE_AND_SCHEMA.md](ARCHITECTURE_STORAGE_AND_SCHEMA.md)）。换代隔离不归本服务：一次 run 一个目录，run 目录由 `run_control` 在锁内经 `runs.allocate` 分配（见 [SERVICE_RUN_CONTROL.md](SERVICE_RUN_CONTROL.md)）。

## 与数据层的分工

```text
app/services/recording/   编排：什么时候拉、顺序、失败怎么办
app/storage/hls/          格式：段文件名、m3u8 文本、fMP4 字节、sidecar、编解码
app/storage/inference/    格式：detections.jsonl 的 record 形状与追加
```

数据层**不持锁**：「同一 `(run, track)` 的段写必须串行」「同一 run 的 detections 追加必须串行」这两条前提都由本服务的队列构造。

## 生命周期与接线

`app/main.py` 的 lifespan 嵌套顺序：`health_monitor → stream → (cleanup, alarm) → recording → inference`。recording 嵌在 inference 外层：inference 停机期间 sweeper 与两条队列仍活着，已攒满的段与检测结果照常被拉走落盘；等 inference 停完，recording 的 `finally` 再停。进程停机不经 `stop_run`，cq 里不足一段的残帧与最后不到 1 s 的检测结果不会被 flush——已接受（`app/services/inference/online/service.py::stop` 注释）。

`start()` 建两条 `SerialTaskQueue`（`"recording"` 写段、`"recording-detections"` 写检测结果）并起 `SegmentSweeper`；`stop()` 先停 sweeper 不再拉新产物，再停两条队列让它们排空——**这个先后不能反**，反过来会在队列停机后继续拉，提交被拒而数据已经从 CQ 弹出去了。两条队列之间没有停机先后要求（写的是不同域的不同文件）。

> `SerialTaskQueue` 是一次性的（`stop()` 后不能再 `start()`），故队列在 `start()` 里建、不在 `__init__` 里建——否则单例跑完两轮 start/stop 就炸。

## 对外成员

| 成员 | 调用方 | 语义 |
|------|--------|------|
| `start()` / `stop(timeout)` | `recording.lifespan()` | 起停两条队列与 sweeper |
| `collect_from(cq)` | SegmentSweeper（唯一） | 取走该 CQ 此刻该落盘的一切 |
| `submit_segment(cq, track, frames) -> bool` | 内部 + 单测 | 打包成段任务入 hls 队列；False = 这段不会被写 |
| `submit_detections(cq, frames) -> bool` | 内部 + 单测 | 打包成检测结果任务入 detections 队列；False = 这批不会被写 |
| `flush_residual(cq, until_ts=None)` | `RunControlService.stop_run` / `collect_from` | 把不足一段的残帧切完落盘，末尾把 `ca_detections` 全排空一并交出 |
| `request_residual_flush(cq, fence_ts)` | `HealthMonitorWorker`（断流时） | **只登记**一次残帧 flush |

三点契约：

- **run 身份由 `cq.run` 带**：打包时取出 `RunIdentity` 放进 `_SegmentJob(run, track, frames)` / `_DetectionJob(run, frames)`，任务不持 cq。`cq.run is None`（裸建 / 未绑定 run）的 CQ 定位不到落盘目录：`submit_*` 返回 False、`flush_residual` / `request_residual_flush` 跳过、`collect_from` 不取帧。
- **入队成功 ≠ 写成功**：真正落盘在队列线程上异步发生。要结果的调用方说明它本就不该异步。
- **`flush_residual` 的 `until_ts`**：`None`（拆除期）= 全排空；给值（断流期）= 只切栅栏之前的帧，重连后的新帧留在队列里等 sweeper 照常拉整段。**栅栏只作用于段**，detections 两种情况下都是全排空。拆除期须在 `cq.close()` 释放帧之前调（`RunControlService.stop_run` 保证）。

## 零锁并发模型

本服务**没有任何锁**。两件正交的事各由一个机制构造：

```text
同一 run 内的顺序    由 SerialTaskQueue 的提交序构造（两条队列各自单消费线程，各自内部提交序 = 执行序）
跨 run 的隔离        由盘上一 run 一目录构造（不归本服务）
```

- 任务只带 `RunIdentity`，写口只写这个 run 的目录。旧一代迟到的段 / 检测结果写进它自己的 run 目录（补全旧录像结尾，新一代读不到）。
- run 目录已被 TTL 回收时，写口抛 `FileNotFoundError`（写者不建 run 目录），`SerialTaskQueue._execute` 记 error 后吞掉，这批丢弃。
- `_pending_flush: {(task_id, step_id) → (cq, fence_ts)}` 有**三个线程**碰：health_monitor 写（`request_residual_flush`）、sweeper 取（`_take_pending_flush`）、拆除路径回收（`flush_residual(until_ts=None)`）。免锁靠 `dict` 的 `__setitem__` / `get` / `pop` 各自是一次原子 C 调用；不对它做逐元素迭代。
- 运行期 CQ 的 drain 者只有 sweeper 一个线程。

### 失败不重试

`_write` / `_write_detections` 各只一次调用（`hls.insert_segment(job.run, …)` / `inference.append_detections(job.run, …)`），抛出的异常由 `SerialTaskQueue._execute` 记 error 后吞掉，本模块**刻意不重试**：

- 会抛的失败都不是瞬时的：ffmpeg 缺失、盘满、权限、run 已回收，重试也修不好。
- `append_detections` 是纯追加，重试会写出重复帧。
- **丢一段 ≈ 丢 10 秒录像；丢一批检测结果 ≈ 丢一个 sweep tick**，都比写坏整个 run 便宜。

### 三条不变式（破了都不报错、只是数据静默损坏）

1. **两条队列都不能加 worker**：同一 run 内相邻段的 tfdt 按执行顺序累计，并发会碰撞。`config/recording_config.yaml` 因此**没有 `workers` 项**。
2. **运行期 CQ 的 drain 者只能有 sweeper 一个**，入口是 `collect_from`；断流走 `request_residual_flush` 登记、由 sweeper 那一轮执行，不要在别的线程直接 drain。
3. **`_pending_flush` 只做单次 dict 操作**（见上），这是它三线程免锁的前提。

## Sweeper 是纯节拍器

`sweep_worker.SegmentSweeper`（包内私有，只由 `RecordingService.start()` 构造，服务以 `_sweep_worker` 持有；线程名 `RecordingSweeper`）每隔 `sweep_interval_seconds` 遍历 `client_service.snapshot()`，对每个活跃 CQ 调一次 `service.collect_from(cq)`。**它只管什么时候拉，不管拉什么**——取哪几条队列、按什么顺序取、挂起的断流请求怎么办，全在 `collect_from`。

它把整个 `cq` 传过去、不拆成 `task_id` / `step_id`：帧归属的 run 就是 `cq.run`，必须在**取帧的那一刻**捕获；晚一步去注册表取，取到的可能已是新一代的 CQ。`_pending_flush` 的身份核对用的也是这个 cq 对象。

`sweep_worker` 不 import 任何单例——`clients` 与 `service` 都是注入的，方向向下。

## PULL 模型与 `collect_from` 的顺序

落盘是 **PULL**：CQ 的 `ca_raw` / `ca_processed` / `ca_detections` 是纯缓冲、不触发落盘，由 sweeper 周期拉取。`collect_from` 的四件事顺序**定死**：

```text
① 拉 raw 整段（take_raw_segment 循环）
② 拉 processed 整段（take_processed_segment 循环）
③ 断流残帧（有挂起请求才做：_take_pending_flush → flush_residual(until_ts=fence)）
④ 拉 detections（submit_detections(cq, cq.drain_ca_detections())）
```

- **③ 必须在 ①② 之后**：残段的帧 ts 晚于本轮所有整段，反过来提交会让清单 ts 逆序；入队序即执行序，而每段的 `tfdt` 是执行时读到的累计 EXTINF——顺序一乱，后写的段就在媒体轴上盖掉先写的，不报错，只是画面丢一截。
- **④ 与 ①②③ 无顺序约束**：走另一条队列、写另一个域的另一个文件，与段的媒体轴无关。③ 发生时 ④ 已由 `flush_residual` 末尾完成，本轮不再 drain 第二次。

## 检测结果落盘（第二条队列）

```text
推理写回口 → cq.append_ca_detections(frame)     只入缓冲，写回线程零 IO；降级帧不入缓冲
sweeper → collect_from ④ → drain_ca_detections → _DetectionJob(run, frames)
        → "recording-detections" 队列 → inference.append_detections(run, frames)
        → {run}/inference/detections.jsonl
拆除：RunControlService.stop_run → flush_residual(cq) 末尾全排空
```

- **为什么两条队列**：段写里有 ffmpeg 转码（单段 0.26–3 s），detections 排在它后面会跟着一起被背压丢，而丢一次就是十几帧检测结果、静默。两条队列互不阻塞，各用 `queue_size` 作上限。
- **为什么缓冲放在 cq 上**：与 `ca_processed` 同形——外部共享的生产者（推理写回线程）→ per-cq 缓存 → sweeper 拉。拉模式不产生 inference → recording 的依赖边，也保住「运行期 CQ 的 drain 者只有 sweeper」。缓冲本身的容量与丢弃口径见 [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)。
- **没有 `until_ts` 栅栏**：栅栏防的是一段视频横跨断流 gap 被 `effective_fps` 反推成慢放；detections 每帧一行、行间无依赖，断流在序列里只是一个 ts 空洞。
- 每 tick 缓冲里有多少交多少（15 fps 检测率下约 15 帧一批），没有「攒满一段」的概念。
- `detections.jsonl` 的唯一写者是本服务；离线 runner 只读它（`inference.read_detections(run)`）。

## 断流残帧链路（消灭 3× 慢放）

同一 run 内 RTSP 断流重连**不拆除 CQ、不清队列**。断流那刻攒在 CA 队列里的半批帧会被重连后的帧补满、拼成横跨 gap 的段，而 `eff_fps` 由首末帧跨度反推、跨度里混进了整段 gap → 10 秒画面写成 30 秒 EXTINF，回放 / 导出 / 送标三条链路一起中招；`eff_fps≈9.99` 仍落在合理带 `[1,60]` 内、不触发退化兜底，**全程无一条报警，每次重连必现**。

```text
HealthMonitorWorker._enter_reconnect_mode
    写 _reconnecting_clients[task_id]
    → recording.request_residual_flush(cq, fence_ts=last_frame_time)   只登记
HealthMonitorWorker._handle_reconnecting_client（判定重连成功时）
    → recording.request_residual_flush(cq, 同一个 fence_ts)             再登记一次
sweeper 下一 tick → collect_from → _take_pending_flush → flush_residual(cq, until_ts=fence)
    → cq.drain_ca_raw(until_ts) / drain_ca_processed(until_ts)  只弹队首满足栅栏的连续前缀
```

时机约束：

- **首次登记在进入重连时，不能等重连成功**：成功的判据就是「已经来了新帧」，那时残批里已混进重连后的帧，段仍横跨 gap。
- **首次登记必须在写 `_reconnecting_clients` 之后**（health_monitor 侧）：已在重连表里的 task 会被直接 `continue`，放在这里 = 每次断流恰好登记一次。
- **重连成功时用同一栅栏再登记一次**：processed 轨由 viz worker 从推理结果渲染，ts 落后 raw 一个推理管线延迟，断流前的 processed 帧可能在首次 flush 之后才入队。栅栏是时间戳、重连后的帧 ts 都大于它，重复登记只会捞走迟到的断流前帧。
- **不能就地 drain**：两次 drain 各自被 CQ 的锁保护、帧不重不漏，但 `submit_segment` 发生在锁外，谁先入队由调度决定——入队序一乱 tfdt 就乱，走队列挡不住这个（队列只保证执行序 = 入队序）。

`_take_pending_flush` 是**包内私有**、唯一消费者是 `collect_from`：公开它就等于让节拍器知道「挂起请求」这回事，连带把顺序不变式搬进定时器。取走时核对**对象身份**——身份不匹配 = 请求属于同一 `(task, step)` 的上一代 CQ，条目直接丢弃。

`_pending_flush` 的回收在拆除路径：`flush_residual(cq, until_ts=None)` 只 pop 属于本 cq 的条目；`stop_run` 持 `lock_for`，期间不会有新一代登记同键，故核对身份即可安全删除。

## 配置

`config/recording_config.yaml` → `RecordingConfig`（扁平 dataclass，两个旋钮；文件不存在或解析失败时用默认值、记日志不抛，出现未知字段则响亮地崩）：

| 项 | 默认 | 语义 |
|----|------|------|
| `queue_size` | 100 | 每条队列各自的排队上限；满了 `submit_*` 返回 False 并告警。**不给无界选项**——无界只是把「丢一段录像」换成「吃光内存」 |
| `sweep_interval_seconds` | 1.0 | 从活跃 CQ 拉产物的扫描间隔。1s ≪ 段周期(≈10s) 且 ≪ CQ 缓冲容量(≈30s) |

**不在这里配的**：`workers`（见不变式 1）、段时长与帧率——段长由 CQ 帧数（`settings.ca_segment_seconds`）触发、段时长由写侧 EXTINF 从帧 ts 自适应反推，配了也是死值且会误导。

## 单例与引用面

单例 `recording_service` 在 `app/services/recording/instance.py`，只许被 `run_control/service.py` / `routers/*` / 本包 `lifespan()` 引用，外加一处具名例外：`app/daemons/health_monitor/worker.py` 在 `_resolve_deps()` 函数体内取它，断流时调 `request_residual_flush`（门禁 `tests/test_import_hygiene.py::test_singleton_reference_surface`）。包内 `sweep_worker` **不** import 它——服务把自己注入给 sweeper。

`__init__.py` 是门面型（docstring + `lifespan()`，零 re-export）：顶层 re-export 会把 `app.storage.hls` 与 client→numpy 那条链摊给每个 import 本包的人。

## 代码来源

- `app/services/recording/{__init__,instance,service,sweep_worker,config}.py`
- `app/services/utils/task_queue.py`（`SerialTaskQueue`）
- `app/storage/hls/`（`insert_segment` / 布局与格式）
- `app/storage/inference/`（`append_detections` / `read_detections`）
- `app/types/run.py`（`RunIdentity`）
- `app/services/client/queues.py`（`take_*_segment` / `drain_ca_*(until_ts)` / `append_ca_detections` / `drain_ca_detections`）
- `app/services/run_control/service.py`（`flush_residual` 调用点）
- `app/daemons/health_monitor/worker.py`（`_enter_reconnect_mode` / `_handle_reconnecting_client` 登记残帧 flush）
- `app/main.py`（lifespan 嵌套顺序）
- `config/recording_config.yaml`
- `tests/test_recording_service.py`、`tests/test_client_detections_buffer.py`、`tests/test_task_queue.py`、`tests/test_storage_hls.py`
