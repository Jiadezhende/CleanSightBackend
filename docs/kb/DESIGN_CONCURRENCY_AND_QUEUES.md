> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 线程安全与异步解耦设计

CleanSight 的实时路径由多线程、多队列和 per-client 状态组成。这部分设计的核心意义有两个方向：

1. 线程安全性：明确哪些状态可共享、由谁读写、用哪把锁保护，避免竞态、错归属和死锁。
2. 异步解耦：把推理、时序、渲染、持久化、外部 IO 拆成独立节奏，避免慢任务卡住实时链路，提高服务可维护性和故障隔离能力。

## 方向一：线程安全性

线程安全设计的重点不是“到处加锁”，而是把共享状态边界划清楚：同一 client 的生命周期变更串行化；不同用途的数据用不同锁；高频热路径尽量少锁；必须同时清理多个状态时固定加锁顺序。

### API 层 per-client 锁

`app/routers/api.py` 使用 `_client_locks: Dict[str, asyncio.Lock]`。

同一 `client_id` 的 `/api/start` 与 `/api/terminate` 串行执行，不同 client 互不阻塞。

覆盖场景：

- 并发启动同一任务幂等返回。
- 同一 client 任务切换触发完整清理。
- terminate 使用同一把锁。
- 不同 client 并发不互相阻塞。

### InferenceManager per-client 锁

`InferenceManager` 使用 `_client_locks: Dict[str, threading.Lock]` 串行化 `set_task()` 和 `remove_client()`。

关键原因：

- TemporalActor finalize 需要在旧任务上下文中归属告警。
- remove 时要先收结算告警，再落盘残余段，再清缓存。

### ClientQueues 锁库存

ClientQueues 中的锁按职责拆分：

- `_task_lock`：task 和 task_started_at。
- `_raw_lock`：raw queue 和 latest raw。
- `_viz_lock`：processed queue 和 latest rendered。
- `_inference_lock`：latest inference。
- `_frontend_lock`：stage 和 latest temporal。
- `_slide_window_lock`：检测滑动窗口。
- `_alarm_lock`：alarm log、seq、gate。

clear 时固定顺序：

```text
_task_lock -> _raw_lock -> _viz_lock -> _inference_lock
-> _frontend_lock -> _slide_window_lock -> _alarm_lock
```

### SPSC 队列

`ca_ready` 是无锁 SPSC deque：

- 单生产者：decoder。
- 单消费者：dispatcher。
- 依赖 CPython GIL 下 deque append/popleft 原子性。

其他共享队列使用明确锁保护。

### 持久化目录锁

HLS 策略对每个 target_dir 使用目录级锁，确保 transcode、playlist append、metadata update 原子执行，避免并发段写入导致 playlist/tfdt 不一致。

## 方向二：异步解耦与防卡死

异步解耦设计的重点是让每类工作按自己的节奏运行。实时链路只传递必要快照或入队任务，慢推理、慢渲染、慢磁盘、慢 HTTP 不直接阻塞上游，从而降低“一个慢点拖死整条链路”的风险。

### 三池解耦

推理、时序、可视化通过 ClientQueues 解耦：

- 推理写 slide_window 和 latest_inference。
- 时序读 slide_window，写 latest_temporal 和 alarm。
- 可视化读 latest_inference/latest_frame/latest_temporal，写 ca_processed/latest_rendered。

这种设计避免时序分析或渲染阻塞 GPU 推理热路径。

### 持久化队列解耦

持久化服务也采用队列解耦。上游实时路径只把任务放入 `PersistenceManager` 的有界队列，慢任务由后台 worker 异步消费：

- `hls_queue` 隔离视频段写盘、ffmpeg fMP4 转码、playlist/keypoints/metadata 更新。
- `alarm_queue` 隔离外部 HTTP 告警上报。
- `HLSWorkerPool` 和 `AlarmWorkerPool` 独立运行，避免告警上报慢拖住 HLS，或视频转码慢拖住告警。
- 队列满会计数并返回失败，是系统背压和容量告警的观察点。

这个设计把实时链路和慢 IO 分开：解码、推理、时序、可视化只承担生产持久化任务的成本，不直接承担磁盘、ffmpeg 或网络调用的不确定延迟。

### 可维护性收益

解耦后，每个模块的职责更窄：

- Stream 只关心拉流和产帧。
- Inference 只关心推理结果和时序告警。
- Visualization 只关心最新快照的渲染。
- Persistence 只关心慢 IO 和重试。
- HealthMonitor 只关心失联、重连和统一清理。

这让性能问题和故障边界更容易定位，也让新增检测点、调整持久化策略、替换外部告警接口时不必重写实时主链路。

## 代码来源

- `app/routers/api.py`
- `app/services/client/queues.py`
- `app/services/inference/core/manager.py`
- `app/services/inference/core/dispatcher.py`
- `app/services/inference/workers/temporal.py`
- `app/services/inference/workers/visualization.py`
- `app/services/persistence/manager.py`
- `app/services/persistence/workers/hls_worker.py`
- `app/services/persistence/workers/alarm_worker.py`
- `app/services/persistence/strategies/hls_strategy.py`
- `tests/test_api_concurrency.py`
