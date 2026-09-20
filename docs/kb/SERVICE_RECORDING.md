> 更新时间：2026-09-20
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# Recording Service（HLS 录制落盘）

`app/services/recording/` 是 **HLS 落盘写侧的唯一生产实现**：把 CQ 里攒好的帧变成盘上一个可播的 HLS 段，产物落 `{root}/{task_id}/{step_id}/hls/`。它只回答**何时拉、按什么顺序写、算哪一代的产物**；格式怎么落盘全在数据层 `app.storage.hls`（见 [DESIGN_STORAGE_LAYER.md](DESIGN_STORAGE_LAYER.md) 与 [DESIGN_HLS_TIMELINE.md](DESIGN_HLS_TIMELINE.md)）。

persistence 里的旧写侧（`hls_pool` + `HLSSegmentSweeper`）代码仍在，但 `PersistenceManager.start()` 不启动它们，见 [SERVICE_PERSISTENCE.md](SERVICE_PERSISTENCE.md) 的护栏一节。

## 与 storage.hls 的分工

```text
app/services/recording/   编排：什么时候拉、谁是这一代、顺序、失败怎么办、留多久
app/storage/hls/          格式：文件名、目录布局、m3u8 文本、fMP4 字节、sidecar、编解码
```

数据层**不持锁、不判断该不该删**：`hls.delete(task, step)` 只执行，「这是不是新一代的首次写入」属 run 生命周期语义，归本服务；「同一 `(task, step, track)` 的写必须串行、且与该 step 的 `delete` 同序」这条前提也由本服务的队列构造。

## 生命周期与接线

`app/main.py` 的 lifespan 嵌套顺序：`health_monitor → stream → persistence → recording → inference`。recording 与 persistence 同一档、同一个理由——`inference.stop()` 会经 `run_control` 交出最后一批 HLS 残段，那时录制队列必须还活着；等它交完，recording 的 `finally` 再停队列把剩下的排空。嵌到 inference 里层会让队列先停、残段提交被拒，而那些帧已经从 CQ 弹出去了，是真丢。

`start()` 建 `SerialTaskQueue` + 起 `SegmentSweeper`；`stop()` 顺序**不能反**：先停 sweeper 不再拉新段，再停队列让它排空。

> `SerialTaskQueue` 是一次性的（`stop()` 后不能再 `start()`），故队列在 `start()` 里建、不在 `__init__` 里建——否则单例跑完两轮 start/stop 就炸。

## 对外 7 个成员

| 成员 | 调用方 | 语义 |
|------|--------|------|
| `start()` / `stop(timeout)` | `recording.lifespan()` | 起停队列与 sweeper |
| `collect_from(cq)` | SegmentSweeper（唯一） | 取走该 CQ 此刻该落盘的一切 |
| `submit_segment(cq, track, frames) -> bool` | 内部 + 单测 | 打包成落盘任务入队；False = 这段不会被写 |
| `flush_residual(cq, until_ts=None)` | `RunController.stop_run` / `collect_from` | 把不足一段的残帧切完落盘 |
| `request_residual_flush(cq, fence_ts)` | `health_monitor._enter_reconnect_mode` | 断流时**只登记**一次残帧 flush |
| `forget_task(task_id) -> bool` | `RunController.stop_run` | 回收该 task 的代次记录 |

三点契约：

- **代次身份整个由 `cq` 带**：`task_id` / `step_id` 不在签名里单列，拆开传只会多三个对不上的机会。`_SegmentJob` 持的 `cq` 只用于判等与记账，从不调它的方法，故不要求它还活着（`close()` 之后照样能判等）。
- **入队成功 ≠ 写成功**：真正落盘在队列线程上异步发生，且可能被代次校验丢弃。要结果的调用方说明它本就不该异步。
- **`flush_residual` 的 `until_ts`**：`None`（拆除期）= 全排空；给值（断流期）= 只切栅栏之前的帧，重连后的新帧留在队列里等 sweeper 照常拉整段。拆除期须在 `cq.close()` 释放帧之前调（RunController 保证）。

## 零锁并发模型

本服务**没有任何锁**（旧写侧的 `_dir_locks` 目录锁已不存在）。两件正交的事各由一个机制构造：

```text
同代次内的顺序    由 SerialTaskQueue 的提交序构造   —— 但队列解决不了换代
跨代次的隔离      由 cq 对象引用判等构造            —— 但校验解决不了乱序
```

支撑它的三块：

1. `_claimed_by: {(task_id, step_id) → cq}` **只被队列那一个线程碰**（`_write` 读写、`_forget` 清）——`forget_task` 走队列而不是当场清，这是零锁成立的最后一块。
2. `_pending_flush: {(task_id, step_id) → (cq, fence_ts)}` 有**三个线程**碰：health_monitor 写、sweeper 取、队列线程回收。免锁靠 `dict` 的 `__setitem__` / `pop` / `list()` 各自是一次原子 C 调用——**逐元素迭代不在此列**，要先 `list()` 快照（否则 `dictionary changed size during iteration` 会被队列吞成一条 error 日志，后面的代次回收整段跳过）。
3. 运行期 CQ 的 drain 者只有 sweeper 一个线程。

### `_write` 的两步

队列任务体 `_write(job)` 先校验代次、再决定要不要自清，然后才 `hls.insert_segment`。

**① 代次校验（乐观锁）**

```text
current is not None and current is not job.cq and current.step_id == job.step_id
    → 这段属于上一代，丢弃
```

`current` = 该 task **此刻**注册的 CQ，`job.cq` = 提交那一刻的。失败动作是**丢弃，不是重读重试**——旧 run 的段塞进新 run 没有任何意义。

**必须连 `step_id` 一起比**：注册表按 `task_id` 索引，但盘上是一个 `(task, step)` 一个目录。同一 task 从第 2 步切到第 3 步确实换了 CQ，可新 CQ 写的是第 3 步的目录，跟手上这批第 2 步的段在盘上根本不冲突。不比 `step_id` 就会把切 step 时 `stop_run` 交出来的上一步残段全丢。

**② 本代次首写自清（懒惰 supersede）**

```text
current is job.cq and _claimed_by.get(key) is not job.cq
    → hls.delete(task, step) 清掉上一代整域产物，再把 key 记到自己名下
```

懒惰而非在 `start_run` 时 eager 删：新 run 若一段都没写出来，用户还能回放上一次的录像。

⚠ **`current is job.cq` 这个前提是防炸的，不是优化**。少了它规则退化成「表里不是我就清」，于是拆除之后才执行的残段（`current` 已是 `None`、代次表已被 `forget_task` 清掉）会把这个 step **刚写完的整段录像删掉**再写。加上之后，清目录只可能发生在这个 CQ 还注册着的时候，残段与迟到段一律只追加、永不删。

**认领是一次性的，落盘是连续的**：`_claimed_by` 只在首写时记一次，此后同代次的段照写不更新——它回答的不是「最后写这个目录的是谁」，而是「本代次在这个 step 上写过第一段了吗」。

### 失败不重试

`_write` 抛出的异常由 `SerialTaskQueue._execute` 记 error 后吞掉，本模块**刻意不包 `GuardedExecutor`**：`hls.insert_segment` 把清单条目排在最后登记（sidecar → init → 段文件 `os.replace` → playlist 追加 → metadata 统计），重试若落在「条目已追加、统计写失败」之后，会往 playlist 写出**重复 EXTINF**，毁掉整个 step 的回放。而现在会抛的失败（ffmpeg 缺失、盘满）基本都非瞬时，重试也修不好。**丢一段 ≈ 丢 10 秒录像，比毁一整段回放便宜。**

### 三条不变式（破了都不报错、只是数据静默损坏）

1. **队列不能加 worker**：tfdt 会碰撞、旧段串进新 run。「表里不可能登记着比自己更新的一代」这条推论建立在「队列 FIFO 且只有一个消费线程」上——新一代的第一次提交必然晚于旧一代的所有提交。`config/recording_config.yaml` 因此**没有 `workers` 项**。
2. **运行期 CQ 的 drain 者只能有 sweeper 一个**，入口是 `collect_from`；断流走 `request_residual_flush` 登记、由 sweeper 那一轮执行，不要在别的线程直接 drain。
3. **`_pending_flush` 的免锁前提**（见上）：逐元素迭代前先 `list()`。

## Sweeper 是纯节拍器

`_sweeper.SegmentSweeper`（包内私有，只由 `RecordingService.start()` 构造）每隔 `sweep_interval_seconds` 遍历 `client_manager.snapshot()`，对每个活跃 CQ 调一次 `service.collect_from(cq)`。**它只管什么时候拉，不管拉什么**——取哪几条队列、按什么顺序取、挂起的断流请求怎么办，全在 `collect_from`。

它把整个 `cq` 传过去、不拆成 `task_id`/`step_id`：代次身份就是这个对象引用，必须在**取帧的那一刻**捕获；晚一步去注册表取，取到的可能已是新一代的 CQ。

`_sweeper` 不 import 任何单例——`clients` 与 `service` 都是注入的，方向向下。

## PULL 模型与 `collect_from` 的顺序

HLS 分段落盘仍是 **PULL**：CQ 的 `ca_raw` / `ca_processed` 是纯缓冲、不触发落盘，由 sweeper 周期拉取（周期拉取者从 persistence 的 `HLSSegmentSweeper` 换成了本服务的 `SegmentSweeper`）。`collect_from` 的三件事顺序**定死**：

```text
① 拉 raw 整段（take_raw_segment 循环）
② 拉 processed 整段（take_processed_segment 循环）
③ 断流残帧（有挂起请求才做：_take_pending_flush → flush_residual(until_ts=fence)）
```

**③ 必须在 ①② 之后**：残段的帧 ts 晚于本轮所有整段，反过来提交会让清单 ts 逆序；入队序即执行序，而每段的 `tfdt` 是执行时读到的累计 EXTINF——顺序一乱，后写的段就在媒体轴上盖掉先写的，不报错，只是画面丢一截。

`cq.step_id is None`（裸建 / 未绑定 step）时**不取帧**：取了就只能丢，留在缓冲里等它绑上 step 才对。

## 断流残帧链路（消灭 3× 慢放）

同一 run 内 RTSP 断流重连**不拆除 CQ、不清队列**。断流那刻攒在 CA 队列里的半批帧会被重连后的帧补满、拼成横跨 gap 的段，而 `eff_fps` 由首末帧跨度反推、跨度里混进了整段 gap → 10 秒画面写成 30 秒 EXTINF，回放 / 导出 / 送标三条链路一起中招；`eff_fps≈9.99` 仍落在合理带 `[1,60]` 内、不触发退化兜底，**全程无一条报警，每次重连必现**。

现在的链路：

```text
health_monitor._enter_reconnect_mode
    写 _reconnecting_clients[task_id]
    → recording.request_residual_flush(cq, fence_ts=last_frame_time)   只登记
sweeper 下一 tick → collect_from → _take_pending_flush → flush_residual(cq, until_ts=fence)
    → cq.drain_ca_raw(until_ts) / drain_ca_processed(until_ts)  只弹队首满足栅栏的连续前缀
```

三条时机约束：

- **登记在进入重连时，不能等重连成功**：成功的判据就是「已经来了新帧」，那时残批里已混进重连后的帧，段仍横跨 gap。
- **登记必须在写 `_reconnecting_clients` 之后**（health_monitor 侧）：已在重连表里的 task 会被直接 `continue`，放在这里 = 每次断流恰好登记一次。
- **不能就地 drain**：两次 drain 各自被 CQ 的锁保护、帧不重不漏，但 `submit_segment` 发生在锁外，谁先入队由调度决定——入队序一乱 tfdt 就乱，走队列挡不住这个（队列只保证执行序 = 入队序）。

`_take_pending_flush` 是**包内私有**、唯一消费者是 `collect_from`：公开它就等于让节拍器知道「挂起请求」这回事，连带把顺序不变式搬进定时器。取走时核对**对象身份**——身份不匹配 = 请求属于上一代 CQ，条目直接丢弃。

## 代次记录回收

`forget_task(task_id)` 由 `RunController.stop_run` 在清 registry 之后调，**走队列而不是当场清**，两个收益：

1. `_claimed_by` 从此只被队列那一个线程碰（零锁的最后一块）。
2. FIFO 保证它排在这一代所有段之后——记录活到最后一段写完才消失，而不是在残段还没落盘时就被抹掉。

排不进去（队列满 / 已停机）只漏一条小壳记录，且下一代首写会重新认领覆盖，故用默认 timeout、**不降级同步执行**（与 `SerialTaskQueue` 文档里「不许丢的任务传大 timeout」相反）。

队列线程上的 `_forget` 两件事各有口径：

- `_claimed_by` **无差别清**（自愈：下一代首写重新认领）。
- `_pending_flush` **必须核对身份再清**。`forget_task` 是异步的，执行前可能堆着几秒的编码/转码，这期间同一 task 完全可能已起新一代并登记了同键请求；无差别清会吞掉新一代的请求，表现正是那个横跨 gap 的慢放段，且静默。

## 配置

`config/recording_config.yaml` → `RecordingConfig`（扁平 dataclass，两个旋钮；文件不存在或解析失败时用默认值、记日志不抛，出现未知字段则响亮地崩）：

| 项 | 默认 | 语义 |
|----|------|------|
| `queue_size` | 100 | 队列排队上限；满了 `submit_segment` 返回 False 并告警。**不给无界选项**——无界只是把「丢一段录像」换成「吃光内存」 |
| `sweep_interval_seconds` | 1.0 | 从活跃 CQ 拉整段的扫描间隔。1s ≪ 段周期(≈10s) 且 ≪ CQ 缓冲容量(≈30s) |

**不在这里配的**：`workers`（见不变式 1）、段时长与帧率——段长由 CQ 帧数（`settings.ca_segment_seconds`）触发、段时长由写侧 EXTINF 从帧 ts 自适应反推，配了也是死值且会误导。

## 单例与引用面

单例在 `app/services/recording/instance.py`，只许被 `run_control` / `routers/*` / 本包 `lifespan()` 引用，外加一处具名例外：`health_monitor/manager.py` 断流时调 `request_residual_flush`（门禁 `tests/test_import_hygiene.py::test_singleton_reference_surface`）。包内 `_sweeper` **不** import 它——服务把自己注入给 sweeper。

`__init__.py` 不做 re-export（门面型）：顶层 re-export 会把 `app.storage.hls` 与 client→numpy 那条链摊给每个 import 本包的人。

## 代码来源

- `app/services/recording/{__init__,instance,service,_sweeper,config}.py`
- `app/utils/task_queue.py`（`SerialTaskQueue`）
- `app/storage/hls/`（`insert_segment` / `delete` / 布局与格式）
- `app/services/client/queues.py`（`take_*_segment` / `drain_ca_*(until_ts)`）
- `app/services/run_control.py`（`flush_residual` / `forget_task` 调用点）
- `app/services/health_monitor/manager.py`（`_enter_reconnect_mode` 登记残帧 flush）
- `app/main.py`（lifespan 嵌套顺序）
- `config/recording_config.yaml`
- `tests/test_recording_service.py`、`tests/test_task_queue.py`、`tests/test_storage_hls.py`
