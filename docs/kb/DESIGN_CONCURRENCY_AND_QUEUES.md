> 更新时间：2026-08-02
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 线程安全与异步解耦设计

CleanSight 的实时路径由多线程、多队列和 per-client 状态组成。这部分设计的核心意义有两个方向：

1. 线程安全性：明确哪些状态可共享、由谁读写、用哪把锁保护，避免竞态、错归属和死锁。
2. 异步解耦：把推理、时序、渲染、持久化、外部 IO 拆成独立节奏，避免慢任务卡住实时链路，提高服务可维护性和故障隔离能力。

## 方向一：线程安全性

线程安全设计的重点不是“到处加锁”，而是把共享状态边界划清楚：同一 client 的生命周期变更串行化；不同用途的数据用不同锁；高频热路径尽量少锁；必须同时清理多个状态时固定加锁顺序。

### 生命周期锁统一到 `ClientManager.lock_for(task_id)`

per-task 生命周期事务锁**收敛为一把** `RLock`，由 `ClientManager.lock_for(task_id)` get-or-create。`RunController.start_run` / `stop_run` 全程持它；api 层（`app/routers/api.py`）不再自持锁，只经 `asyncio.to_thread` 把同步持锁段桥出事件循环调 `run_controller`；HealthMonitor 后台线程也走同一把锁。三方共用消除「HM 迟到 cleanup 误删 /start 刚建 CQ」的竞态。RLock 可重入（start_run 持锁内重启时再调 stop_run 不自死锁）。

`InferenceManager` 不自持 per-client 锁：`start_workflow` / `stop_workflow` 的互斥由上层 `lock_for(task_id)` 承接。TemporalActor finalize 在旧 CQ 不可变身份上归属告警，无需「先停旧 actor 再切字段」的排序。

覆盖场景：并发启动同一任务幂等返回；改 step/URL 触发停旧全量重建；terminate 与 start 共用同一把锁；不同 task 并发不互相阻塞。

### CQ 状态机写门 + 对象身份 fence

跨 run 隔离靠两道机制，不靠 run_epoch：

- **写门**：CQ 有单调状态机 `ACTIVE→DRAINING→CLOSED`（`_state_lock` 仅串行转换，不与 payload 锁互嵌）。所有写在**写入时刻**判 state，迟到写（decoder 抽帧 / 结果写回 / tick）落到 DRAINING/CLOSED 的旧 CQ 被拒，不串台到同键新 run。settlement 告警与 HLS flush 在 DRAINING 仍放行（非对称门）。详见 [SERVICE_CLIENT_STATE.md](SERVICE_CLIENT_STATE.md)。
- **对象身份 fence**：`ClientManager.remove_if(task_id, expected_cq)` 仅当槽位 `is expected_cq` 才删；`RunController.stop_run(expected=...)` 在拆除前核对槽位仍是当初捕获的 CQ，否则整段放弃，防 HM 误删被 /start 抢占重启的新 run。

### ClientQueues 锁库存

ClientQueues 中的锁按职责拆分（身份 `task_id/step_id/stage/task_started_at` 为构造定死的不可变
primitive，热路径免锁直读，故**无** `_task_lock`）：

- `_raw_lock`：raw queue 和 latest raw。
- `_viz_lock`：processed queue 和 latest rendered。
- `_inference_lock`：latest inference（帧级 `FrameFeature` 原子快照）。
- `_frontend_lock`：latest temporal。
- `_slide_window_lock`：帧级 `FrameFeature` 滑窗（一帧一条，多流已对齐）。
- `_alarm_lock`：alarm log、seq、gate。

clear 时固定顺序（6 把 payload 锁）：

```text
_raw_lock -> _viz_lock -> _inference_lock
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

### 线程与实例生命周期审计（已落地结论）

一次全仓线程/实例生命周期审计（创建/销毁时机、持有关系、关停顺序）落地 4 处改动并经远程真实流验证（`CleanSightBackend-test`，2026-06-27）。方法论：**先量再改，不为臆想风险买单**——两处「以为是 bug」经核实后被证伪或收窄。稳定结论：

**三种生命周期粒度**（关停编排入口 [main.py](../../app/main.py) lifespan：health 套 ai，逆序关——先停推理消费侧、再停流生产侧）：

| 粒度 | 实例 | 创建 → 销毁 |
|------|------|-------------|
| 进程级单例 | `stream_service`/`persistence_manager`/`InferenceManager`/`GlobalHealthMonitor` | import·lifespan → lifespan 关闭 |
| stage 级常驻线程/进程 | dispatcher/推理子进程(`RemoteInferProxy` spawn)/viz worker/HLS·Alarm 池/cleanup/selector | service `start()` → `stop()` |
| per-run 动态实例 | TemporalActor/FFmpegDecoder | `start_workflow`·`start_stream` → `stop_workflow`·`stop_stream` |

**唯一真正不可中断点 = 推理子进程内 `StageWorker` 的 GPU 前向（CUDA 同步）**：进程隔离后 GPU 前向不在主进程线程里，主进程 `stop_event` 管不到子进程内的前向。`RemoteInferProxy.stop()` → `_kill_child()` 用 `terminate→join(2.0)→kill→join(2.0)` 硬收尸（镜像 decoder.py，见下），CUDA 半途的前向随进程被杀、不残留孤儿。收益是主进程再无 in-thread CUDA 同步点——旧模型「daemon `join(2.0)` 超时后强杀 GPU 半途线程」的风险已随隔离消失；主进程侧 collector/supervisor/dispatcher 等守护线程都真可中断（`stop_event.wait(interval)` 或带超时 `queue.get`）。

**关键副作用不依赖被 join 的 worker**：两条 flush 路径（terminate 侧 `stop_workflow`→`feature_store.close(cq)` 只刷当前 `(task,step)`；lifespan 侧 `InferenceManager.stop()`→`feature_store.flush()` 全量兜底未走正常结束的 run）均跑在**控制/调用线程**，故即便推理子进程被硬杀，落盘已同步发生——「硬杀下仍安全」的正面佐证。FeatureStore 同步落盘（实测 max 3.6ms）是有意选择，非缺陷。

**decoder 直接 SIGKILL**（[decoder.py](../../app/services/stream/decoder.py) `stop()`，2026-06-26 落地）：弃 `terminate→wait(2.0)→kill` 三级降级，改 `kill→wait(reap)`。实测卡读时优雅路径白耗 2007ms 后照样 SIGKILL，直接 kill 仅 2ms（`stop_stream` 全程 12.9ms）。零新增风险：ffmpeg 只解码到 `pipe:1` 不写文件（强杀无产物损坏）、RTSP 对端是自有 mediamtx_gateway（断连即回收、不需优雅 TEARDOWN）。副产物：`_stop_decoder_async` fire-and-forget 线程存活 ~2s→~ms，L1-a 线程堆积自愈。

**审计清理的死代码/泄漏**（已落地）：`InferenceManager` 的 per-client `defaultdict(Lock)` 慢泄漏 → 收敛为 `lock_for(task_id)` 单一生命周期锁（天真 pop 会与并发 `start` 撞 race，故不做引用计数回收）；未用的 `ThreadPoolExecutor`（零 `.submit()`）+ 误导性 `num_worker_threads` 删除；`_refresh_thread`（旧 `ClientRefreshThread`，dispatcher 直引单例 ClientManager 后冗余）删除。

**备查（现非问题）**：模型 / CUDA 上下文在 `stop()` 不显式释放——关进程时驱动回收无碍；若将来做「不退进程的重启/换模型」会变真泄漏。selector 优雅停（`shutdown` 未 `set` `_stop_event`）为 cosmetic，OS 兜底关 fd。

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

- `hls_queue` 隔离视频段写盘、ffmpeg fMP4 转码、playlist/metadata 更新。
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

## 锁设计原则（可复用方法论）

沉淀自纯 `threading` 架构，可复用于其他模块。基础方法论：**自底向上构建线程安全**——底层组件（`ClientQueues`）每个方法各自原子、不依赖调用方持外锁；上层只为「多步组合序列」加锁，不重复保护底层已安全的字段。核心思路：**按访问模式分锁，不按资源分锁**——同一业务动作总一起读写的字段归同一把锁（如 `_viz_lock` 合并 `ca_processed`+`_latest_rendered`，VizWorker 一次加锁写两者）。

1. **识别 SPSC，消除不必要的锁**：单写单读且角色固定时，GIL 已保证 `deque.append/popleft` 原子（`ca_ready`：decoder 唯一写、dispatcher 唯一读，无锁）。能证明不需要锁就不加。
2. **快照模式避免锁嵌套**：热路径读多把锁保护的字段时，先在轻锁下快照到局部变量再进重锁，两锁生命周期不重叠——同时消除 TOCTOU。
3. **固定全清顺序防死锁**：同时持多锁（`clear()`）时死锁充要条件是不同路径乱序取锁；在类 docstring 声明唯一顺序，用 `contextlib.ExitStack` 顺序加锁、逆序释放。
4. **关联值同临界区读**：存在不变式的两值必须一次加锁同读。例：`get_alarm_snapshot` 单 `_alarm_lock` 内返回 `(增量告警, max_seq)`，保证 `max_seq ≥ max(a.seq)`，游标不漏告警。
5. **赋值与副作用分离**：setter 只赋值，缓存清理/事件触发等副作用拆到显式方法（合约写 docstring）。本系统更进一步——**CQ per-run 不可变**：身份构造定死、无 setter 副作用，清理走 `close()`/`_release_payload`。
6. **状态机转换先退旧再进新**：生命周期切换严格「旧状态完整退出→再建新」。本系统由 `RunController` 保证（`to_draining`→停 decoder/actor→建新 CQ），per-run 不可变让 settlement 归属天然正确，无需「先停旧 actor 再切字段」的隐式排序。
7. **按业务层级纵深分锁**：不同调用来源需各自锁层——api 协程经 `asyncio.to_thread` 桥出、服务层 `lock_for(task_id)` RLock 串行事务、数据层细粒度锁护读写。关键：`asyncio.Lock` 管不住独立 `threading.Thread`（HealthMonitor），故服务层 RLock 是必要纵深，非冗余。
8. **幂等语义精确到「完全相同」**：不能只查主键。`RunController.start_run` 仅当 `step_id` 与流 URL 均不变才幂等返回，否则全量停旧重建——低频生命周期操作，全量重建的简单性优于部分更新的边界复杂度。

每个有锁的类应在 docstring 维护「锁清单 + 全清顺序」（`grep` 锁名即可验证代码与文档一致），见 `ClientQueues` docstring。

## 代码来源

- `app/routers/api.py`
- `app/services/run_control.py`
- `app/services/client/manager.py`（`lock_for` / COW / `remove_if`）
- `app/services/client/queues.py`（`RunState` 状态机 + 锁库存）
- `app/services/inference/manager.py`
- `app/services/inference/detection/dispatcher.py`
- `app/services/inference/temporal/actor.py`
- `app/services/inference/visualization/worker.py`
- `app/services/persistence/manager.py`
- `app/services/persistence/workers/hls_worker.py`
- `app/services/persistence/workers/alarm_worker.py`
- `app/services/persistence/strategies/hls_strategy.py`
- `app/main.py`（lifespan 关停编排）
- `app/services/stream/decoder.py`（SIGKILL `stop()`）
- `app/services/inference/detection/service.py`（`_inference_loop` + join 后内联 `is_alive` 诊断）
- `tests/test_api_concurrency.py`
- `tests/test_teardown_identity_fence.py`
