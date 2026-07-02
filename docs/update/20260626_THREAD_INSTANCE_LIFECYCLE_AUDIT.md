# 线程与实例生命周期审计

> **变更状态**：生效中（2026-06-27）　<!-- 审计完成：4 项改动已落地并经远程真实流验证；L1-b/模型释放为低优先/备查 -->
> **知识库**：待沉淀
>
> 相关：[BOUNDARY_LAYER_DESIGN.md](../BOUNDARY_LAYER_DESIGN.md)（异常/重启边界层）、[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)（分层数据流）。

## 概述

- **做什么**：盘点全仓所有线程与有状态实例的生命周期（创建时机、销毁时机、持有关系、关停顺序），逐模块核实是否存在泄漏 / 卡死 / 半关闭无证据等问题。
- **为什么**：由 FeatureStore「同步 vs 异步」一问发散到「全仓并发/实例生命周期是否统一」。结论是：**好模式都已散落在仓库里，只是没统一**。
- **影响面**：stream / inference / persistence / health_monitor 四个服务的 start/stop，main 层 lifespan 编排，以及 per-client 动态实例的回收。

> 本文为**审计追踪**，不是已落地的改动记录。每个问题项在「进度清单」里带状态；确认并修复后再回填对应小节。

## 全景：三种生命周期粒度

| 粒度 | 实例 | 创建时机 | 销毁时机 |
|------|------|---------|---------|
| **进程级单例** | `stream_service`、`persistence_manager`、`InferenceManager`、`GlobalHealthMonitor` | import / lifespan 启动 | lifespan 关闭 |
| **stage 级常驻线程** | dispatcher、各 InferWorker、visualization worker、HLS/Alarm worker 池、cleanup、selector | service `start()` | service `stop()` |
| **per-client 动态实例** | TemporalActor、FFmpegDecoder、`_client_locks`、`_encoded_cache` | `set_task` / `start_stream` | `remove_client` / `stop_stream` |

**编排入口** [main.py:63-70](../../app/main.py#L63-L70)：`health.lifespan()` 套 `ai.lifespan()`，后者 `ai.start()` → `InferenceManager.start()` 拉起整条推理链；`finally` 里 `ai.stop()` + `stream_service.shutdown()`。
关闭顺序正确（逆序：先停推理消费侧，再停流生产侧），但**同步串行**（见系统性问题 #3 的衍生：main 层串行 stop 可借鉴 TemporalActor 的两阶段模式）。

## 逐模块核实

### 模块 1：Stream 服务 ★★★★☆

持有：1 个常驻 `_selector_thread`（[service.py:88](../../app/services/stream/service.py#L88)）+ N 个 per-client `FFmpegDecoder`（各含 stderr/reader 子线程 + ffmpeg 子进程）。

**亮点（刻意保留，勿动）：**
- `stop_stream` [service.py:303-332](../../app/services/stream/service.py#L303-L332)：锁内 pop decoder + 注销 selector（防并发重复操作），**锁外**异步停进程（避免 2s 阻塞持锁）。教科书写法。
- `shutdown()` [service.py:660+](../../app/services/stream/service.py#L660)：原子取走 decoders 再逐个同步 stop，进程退出前确保 ffmpeg 子进程被回收。
- `decoder.stop()` 有 `terminate → wait(timeout) → kill` 三级降级 [decoder.py:190-218](../../app/services/stream/decoder.py#L190-L218)，子进程一定能收。

**问题：**
- ⚠️ **[L1-a] `_stop_decoder_async` fire-and-forget 临时线程**（[service.py:379-382](../../app/services/stream/service.py#L379-L382)），无 join、无引用。**复核降级**：其做的活其实有日志（kill warning + worker 的 BoundaryLayer1 异常捕获），故"工作结果可观测"；真正不可观测的只剩"瞬时存活线程数"，且线程做的是有界活、卡不住 → 近乎纯理论项。
  - **关联改动（已落地 2026-06-26）：decoder 改直接 SIGKILL**，见下「保留项/调优」。这使每个 `stop-decoder-*` 线程存活时间从 ~2s 暴跌到 ~ms，L1-a 那点瞬时堆积自愈，无需再单独处理。

> **[决策] decoder.stop() 弃优雅 terminate，直接 SIGKILL（已落地）**
> [decoder.py stop()](../../app/services/stream/decoder.py#L190)：原 `terminate → wait(2.0) → kill` 三级降级，改为 `kill → wait(reap)`。
> - **实测**：ffmpeg 阻塞在读上时，优雅路径白耗 **2007ms** 后照样 SIGKILL；直接 kill **2ms**（响应态：19ms→5ms）。
> - **零新增风险**：① 此 ffmpeg 仅解码到 `pipe:1`，不写文件，强杀无产物损坏；② RTSP 对端是**自有 mediamtx_gateway**，对断连(RST)超时回收会话，不需优雅 TEARDOWN；③ 拆流多在流已不健康时，ffmpeg 卡死 socket 读、SIGTERM 本就无效。
> - 现状本就"did not exit gracefully → killing"，即多数情况已在发 SIGKILL，本改动只是省掉前面那 2s 白等。
- ⚠️ **[L1-b] `shutdown()` 不停 `_selector_thread`**：没 `_stop_event.set()`，靠 `daemon=True` 跟进程一起死；优雅关闭阶段它还在 0.05s 空转轮询。无害但不干净。

### 模块 2：Inference 服务 ★★★★☆（最复杂，四个独立时钟）

**2a. Dispatcher** [dispatcher.py:68-88](../../app/services/inference/core/dispatcher.py#L68-L88)：标准 Event + daemon + `join(2.0)`，`start()` 里 `_stop_event.clear()` 支持重启。规范。

**2b. ModelWorkerService** [service.py:129-180](../../app/services/inference/core/service.py#L129-L180)：
- 持有 1 个 `ThreadPoolExecutor` + 每 stage 1 个 InferWorker（`guarded_run` 包裹，边界层 1 自动重启）。
- ✅ **[L2-a] `self.executor` 是未使用的死代码——已删除（2026-06-26）**：
  - **复核结果**：原以为「`executor.shutdown(wait=True)` 无超时会被卡死的 GPU future 无限阻塞」。核实后**风险不存在**——全仓零 `.submit()`，executor 从没派过活。CPython ThreadPoolExecutor 惰性建线程，无提交即零线程，`shutdown` 瞬间返回。
  - **真实结构**：推理跑在**手动建的 `_worker_threads`**（每个有模型的 stage 一条，[service.py](../../app/services/inference/core/service.py) `start()` 内 `for stage in worker_pools`），`_inference_loop` 同步直调 `worker_pool.infer_batch(batch)`。`executor` 与 `num_worker_threads` 是误导性残骸（注释"每个 stage 一个推理线程"实为谎言，真实线程数 = stage 数，与该参数无关）。
  - **本次处理**：删 `ThreadPoolExecutor` import、`self.executor` 创建 + `stop()` 里的 `shutdown`、以及连带无用的 `num_worker_threads` 参数（含 manager 调用点）。语法自检通过。
  - **遗留**：卡死 GPU 的真实暴露点是 `infer_batch` 同步跑在手动线程上 → `stop()` 的 `thread.join(timeout=2.0)` 超时后被 daemon 强杀。该路径**已有 2s 上界**，缺的只是超时后的 `is_alive()` 证据——归入系统性问题 #3。

**2c. TemporalActor（per-client）** [temporal.py:34-71](../../app/services/inference/workers/temporal.py#L34-L71)：
- 全仓最讲究的一处：拆 `signal_stop()`（只置位）和 `finalize_and_stop()`（置位+join+收结算告警）两步。
- `InferenceManager.stop()` [manager.py:550-560](../../app/services/inference/core/manager.py#L550-L560) 据此做**两阶段关闭**：先对所有 actor 并行 `signal_stop()`，再逐个 `finalize_and_stop()`，N 个 actor 的 join 并行收敛而非 N×2s 串行。**值得反向推广到 main 层。**

**2d. InferenceManager（编排者）** [manager.py:522-590](../../app/services/inference/core/manager.py#L522-L590)：
- `stop()` 顺序：停模型服务 → 两阶段停 actor → `feature_store.flush()` → 停可视化 → 停持久化。（FactLedger 离线异步写，已从在线 manager 摘除，不在此 flush。）

> **[澄清] 两条独立的 flush 路径，均在控制线程同步执行（非 worker 收尾）**
>
> | 触发点 | 调用链 | flush 范围 | 执行线程 |
> |---|---|---|---|
> | **任务结束**（terminate） | `remove_client` → [manager.py:353-354](../../app/services/inference/core/manager.py#L353-L354) `feature_store.close(task_id, step_id)`（= `flush(task_id, step_id)`） | 只刷当前 (task,step) | terminate 请求线程 |
> | **进程终止**（lifespan 关闭） | `InferenceManager.stop()` → `feature_store.flush()`（无参） | 全量残留 | lifespan 线程 |
>
> - 二者**互补**：正常结束各自 close 已清空；`stop()` 的全量 flush 只为"被叫停时仍活跃、未走 remove_client"的 client 兜底（否则 offline 读到的特征尾部被静默截断）。全部正常结束时 `stop()` 的 flush() 啥也刷不到。
> - **关键**：两处都跑在**调用者/控制线程**，不在被 join 的 worker 里 → 即使 worker 被 daemon 硬杀，关键落盘也已同步发生。这是"本套关停在 daemon 硬杀下仍安全"的正面佐证，也印证 **#3 的 `join_or_warn` 仅是诊断**——关键副作用本就不依赖被 join 的线程完成。
> - 边角：任务内**换 step** 不显式 close 旧 (task,step)；但 offline `load()` 读前先 `flush(该格)` + stop() 全量兜底，不丢数据。
- per-client 互斥：`set_task`/`remove_client` 都在 `_client_locks[client_id]` 下执行，settlement 告警归属处理得细（先停旧 actor 再切字段，保证告警落旧任务）。状态机正确。

**问题：**
- ✅ **[L2-b] `_client_locks` 泄漏——已改为单把全局锁（2026-06-26）**：
  - **原问题**：`defaultdict(threading.Lock)`（旧 manager.py:71），每个连过的 `client_id` 永久驻留一把 Lock，从不回收 → 慢速内存泄漏（几万 client 显形）。
  - **为何不能天真 pop**：这把锁守护的就是 `remove_client` 自身；在 `_remove_client_locked` 尾部 pop 会与正阻塞等锁的并发 `set_task` 撞 race —— `defaultdict` 会当场新建另一把锁，**set_task/remove_client 互斥当场失效**，把慢泄漏换成状态机错乱。正确回收需 guard 锁 + 引用计数（用时建、refcount→0 清），但那是为并行度买单的复杂度。
  - **锁的本质 = 控制面**：它只串行单 client 的「先停旧 actor→切 cq.task 字段→建新 actor」时序（保证结算告警归属旧任务），保护 `self._actors[client_id]` 与 cq 任务态。**不在帧推理热路径上**（全仓仅 set_task / remove_client 两处获取）；这些是每会话几次、毫秒级的稀疏事件。
  - **结论**：跨 client 同刻启停的并行度几乎不存在，per-client 锁不值得。改为单把 `self._client_lifecycle_lock = threading.Lock()`，两处 `with self._client_locks[client_id]:` → `with self._client_lifecycle_lock:`，删 `defaultdict` import。**一次性消掉泄漏 + 回收 race 两个问题**，trivially correct；代价仅"不同 client 控制操作串行"，落在毫秒级稀疏路径上，无感。语法自检通过。
- ✅ **[L2-c] `_refresh_thread` 死代码——已删除（2026-06-26）**：
  - **溯源**：原是 `_client_refresh_loop`（`ClientRefreshThread`），周期调 `model_worker_service.refresh_client_queues()` 维护 worker 侧客户端队列快照（即"不断获取客户端状态"）。
  - **下线**：`377582d`「dispatcher 统一引用单例 ClientManager，不独立维护快照」使其冗余（先标 `(redundant)` 仍空转）；`6fd993a`「清理工厂类」删除了 loop 方法体、`refresh_client_queues` 调用、线程创建/启动整段——**但漏删了声明与 `stop()` join 守卫**。
  - **功能未丢**：刷快照的活已被「dispatcher 直引单例 ClientManager」单一真源替代；`refresh_client_queues`/`_client_refresh_loop`/`ClientRefreshThread` 全仓零残留。
  - **本次处理**：删 [manager.py](../../app/services/inference/core/manager.py) 中 `_refresh_thread = None` 声明 + `stop()` 末尾 `if ... is not None: join` 守卫块，补完 `6fd993a` 的清理，零行为影响。语法自检通过。

### 模块 3：Persistence 服务 ★★★★★（最规范，无明显问题）

[manager.py:84-100](../../app/services/persistence/manager.py#L84-L100)：`start()` 拉起 HLS 池 + Alarm 池 +（条件）cleanup；`stop(timeout=10)` 逐个停且等队列清空。HLS/Alarm 池各自 `stop(timeout)` 带超时 join（[hls_worker.py:120-126](../../app/services/persistence/workers/hls_worker.py#L120-L126)）。cleanup 用 `_stop_event.wait(interval)` 可中断 sleep（[cleanup_worker.py:54](../../app/services/persistence/workers/cleanup_worker.py#L54)），关闭时不干等整个 interval。

### 模块 4：Health Monitor ★★★★☆

[monitor.py:101-124](../../app/services/health_monitor/monitor.py#L101-L124)：单后台线程。
- ✅ `start()`/`stop()` 均有 `is_alive()` 幂等防重入（[monitor.py:103](../../app/services/health_monitor/monitor.py#L103)、[monitor.py:120](../../app/services/health_monitor/monitor.py#L120)）——别处都缺的好习惯。
- ✅ 循环用 `_stop_event.wait(timeout=interval)` 可中断 sleep。
- ✅ 构造注入 `client_manager`/`stream_service`/`inference_manager` 三单例（[monitor.py:50-52](../../app/services/health_monitor/monitor.py#L50-L52)）——全仓最接近 DI 的写法，测试友好。
- ⚠️ **[L4-a] `join(5.0)` 后不检查 `is_alive()`**，卡死线程静默漏过（同 main 通病，归入系统性问题 #3）。

## Worker 可中断性 & 资源释放清点（2026-06-27）

逐条核实每个常驻循环：`set()` 后能否及时退出（可中断性），以及持有的资源是否释放。

| 线程 | 阻塞点 | 可中断? | 持有资源 / 释放 |
|---|---|---|---|
| health_monitor `_monitor_loop` | `stop_event.wait(interval)` | ✅ 旗 | 无外部资源 |
| cleanup_worker `_run` | `stop_event.wait(interval)` | ✅ 旗 | 文件扫描，无常驻句柄 |
| dispatcher `_dispatch_loop` | 非阻塞 deque pop + `wait(interval)` | ✅ 旗 | 无 |
| visualization `run` | CPU `_tick` + `wait(sleep)` | ✅ 旗 | 无（读快照） |
| TemporalActor `_run` | `stop_event.wait(tick)` | ✅ 旗 | analyzer/judge；`finalize_and_stop` 控制线程落库 ✅ |
| hls_worker `run` | `queue.get(timeout=0.5)` | ✅ 超时 | 每段 ffmpeg 转码子进程，同步内完成 ✅ |
| alarm_worker `run` | `queue.get(timeout=0.5)` + 停后 drain | ✅ 超时 | 网络上报；drain 防丢告警 ✅ |
| decoder `_stderr`/`_reader` | 阻塞 `readline/read` | ⚠️ 靠杀进程+关管道（非旗） | 管道，stop() 关闭 ✅（SIGKILL 后立即解开） |
| **inference `_inference_loop`** | **同步 `infer_batch`（CUDA）** | 🔴 **此窗口不可中断** | YOLO 模型 + CUDA 上下文，**stop 不显式释放** |
| stream `_selector_loop` | `select(timeout=0.05)` | ✅ 旗（但旗没被举，见 L1-b） | selector fd；`finally:_cleanup_selector` |
| gateway `_cleanup_loop` ×2 | `while True: time.sleep()` | ⚠️ 无停机制 | 纯内存 dict，无需释放 |

**三档结论：**
- **✅ 7 条真可中断**（`wait`/带超时 `get`）：join 安全甚至近仪式，无需动。
- **🔴 唯一真风险 = `_inference_loop` 的 `infer_batch`（CUDA 同步）**：CUDA 调用 wedge 时 `stop_event` 看不到、`join(2.0)` 超时 → daemon 硬杀在 GPU 半途。**无法根治**（不能中断 CUDA、不能强杀线程）；能做的只有 ① 此处加 `join_or_warn` 诊断，② 关键副作用已在控制线程 flush（硬杀不丢数据）。**这是 #3 唯一该落的点。**
- **⚠️ 两个无害瑕疵**：selector cleanup 因 L1-b 不可达（OS 兜底关 fd）；gateway 两个 `while True:sleep` 无停机制但只动内存 dict、进程级单例 → 硬杀无损。

**遗留备忘：模型 / CUDA 上下文在 `stop()` 不显式释放** —— 进程退出时驱动回收，对"关进程"无碍；若将来做**不退进程的重启/换模型**，会变成真泄漏。现非问题，记此备查。

## 系统性问题（按严重度）

| # | 问题 | 位置 | 影响 | 状态 |
|---|------|------|------|------|
| 🔴 1 | `_client_locks` 永不回收 | [manager.py:71](../../app/services/inference/core/manager.py#L71) | 长跑慢泄漏，确定性 bug | ✅ 已修（改单把全局锁，见 L2-b） |
| ⚠️ 2 | ~~`executor.shutdown(wait=True)` 无超时~~ → 实为未用死代码 | service.py | 风险不存在（零 submit）；executor 已删 | ✅ 已修（删死代码，见 L2-a） |
| ⚠️ 3 | join 后从不检查 `is_alive()` | main / 各 `stop()` | 半关闭无证据 | ✅ 已修（清点后仅 `_inference_loop` 1 处真有风险，内联 `is_alive`+warning，不造 helper） |

## 好模式已存在，只是没统一

| 模式 | 当前唯一出处 | 应推广到 |
|------|------------|---------|
| 幂等防重入（`is_alive()`） | health monitor | 各 service `start/stop` |
| 两阶段 signal→finalize 关闭 | TemporalActor | main 层串行 stop |
| 构造注入而非 import 单例 | health monitor | 其余服务（测试友好，长期） |

## 进度清单（逐步推进）

| 项 | 内容 | 状态 | 下一步 |
|----|------|------|--------|
| L2-c | `_refresh_thread` 死代码 | ✅ 已删除 | 完成（补完 6fd993a 清理） |
| L2-b | `_client_locks` 泄漏修复 | ✅ 已修 | 完成（改单把 `_client_lifecycle_lock`） |
| L2-a | executor（实为死代码）清理 | ✅ 已删除 | 完成（连带删 num_worker_threads） |
| 可中断性清点 | 全部常驻循环逐条核实 | ✅ 完成 | 见上「Worker 可中断性 & 资源释放清点」 |
| #3   | join 可见性（诊断性） | ✅ 已落（内联） | 仅 1 处→不造 `join_or_warn` helper（避免为单点抽象）。直接在 `ModelWorkerService.stop()` join 后内联 `if thread.is_alive(): logger.warning(...)`。仅告警、不回收。 |
| L1-a | stop-decoder 线程churn | ✅ 自愈 | decoder 改直接 SIGKILL，线程存活 ~2s→~ms |
| L1-b | selector 优雅停 | 💤 低优先(cosmetic) | shutdown 没 set `_stop_event`→cleanup finally 不可达；OS 兜底关 fd，无损 |
| 遗留 | 模型/CUDA stop 不释放 | 📝 备查 | 关进程无碍；不退进程重启才会泄漏 |
| L4-a | health monitor join 检查 | 合并入 #3 | — |

## 落地小结（实际执行）

按"先核实 → 再动手"逐项推进，最终落地 4 处代码改动 + 2 项降级/备查：

1. ✅ **L2-c** `_refresh_thread` 死代码删除（补完 6fd993a 清理）。
2. ✅ **🔴1/L2-b** `_client_locks` 改单把 `_client_lifecycle_lock`（控制面锁，消泄漏+回收 race）。
3. ✅ **⚠️2/L2-a** `executor` 经复核为未用死代码 → 删除（"无超时阻塞"风险证伪）。
4. ✅ **L1-a** decoder 改直接 SIGKILL（teardown ~2s→~ms，stop-decoder 线程 churn 自愈）。
5. ✅ **#3/L4-a** 可中断性清点后，仅 `_inference_loop` 1 处真有风险 → 内联 `is_alive`+warning，不造 helper。
6. 💤 **L1-b**（selector 优雅停，cosmetic）/ 📝 **模型·CUDA 不释放**（备查，仅"不退进程重启"才泄漏）—— 未动。

> 方法论印证：两处"以为是 bug"（⚠️2 无超时阻塞、#3 需全仓推广）经核实/清点后均被收窄或证伪 —— **先量再改，不为臆想的风险买单**。

## 验证（远程真实流，2026-06-27）

环境：远程 GPU 机 `CleanSightBackend-test`，真实 RTSP（经自有 mediamtx_gateway），client=test.s111。

| 验证项 | 场景 | 结果 |
|----|------|------|
| decoder SIGKILL（terminate 路径） | `POST /api/terminate` | ✅ `killing ffmpeg pid=...` → stop_stream 全程 **12.9ms**（旧路径卡流时 ~2s）；无 `did not exit gracefully` |
| decoder 关停跳过 kill（进程已退场景） | 关进程时 gateway 先停→ffmpeg 自退 | ✅ `poll()!=None` 正确跳过 kill，直接关管道 `decoder stopped`，无误杀日志 |
| 删 executor 不破坏 stop() | 关进程 | ✅ `ModelWorkerService stopped` 正常输出 |
| `is_alive` 诊断（沉默=健康） | **任务运行中** Ctrl-C 关进程 | ✅ 各 InferWorker 2s 内干净退，**未出现** `未在 2s 内退出` warning → worker 可中断实锤（mid-task 也秒退）|
| 关进程结算告警 / 控制线程副作用 | mid-task 关进程 | ✅ 两阶段 `finalize_and_stop` 跑通：`Settlement alarm ... 完成 0 次` + `告警上报成功` |
| 单锁 set_task/remove_client | 正常 terminate + 关停 | ✅ 无死锁、无异常、逆序优雅关停 |
| 单锁并发 start/terminate 压测（item 3） | 同 client 并发 + 跨 client 并发 | ⏳ 未跑（单锁为"更简单"方向、关停已证无死锁，低风险） |
| `is_alive` warning 真触发 | 需 wedged CUDA >2s | ➖ 无法人为构造；**沉默即合格**，非待办 |

> 关停顺序经观察为逆序：ModelWorker → 结算/告警 → persistence → InferenceManager → StreamService(decoder) → HealthMonitor，consumer 先于 producer，无 hang/error。
>
> 注：`StageAwareDispatcher.queue_depth` 报错为远程代码未同步（本地已提交 45edad8），`git pull` 即解，非本次改动引入。
