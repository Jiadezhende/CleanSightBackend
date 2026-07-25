# Inference 分层改造后的最新数据流(L1→L2→L3→L4 全链路通信)

> **变更状态**：生效中（2026-06-20）　<!-- 分层改造 Phase 0/1/2 已落地于 refact/layered-infer：2ee4624 / 105bb09 / 9e31304 -->
> **知识库**：已沉淀 → [kb/SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)(2026-07-21)
> **更新（2026-06-20，online/offline 分离）**：online/offline 彻底分离落地——实时链路不再落盘事实(删 temporal 的 FactLedger 写)、`TemporalAnalyzer` 收窄为纯实时 `run(window)->List[EventFact]`；FeatureStore/FactLedger 落盘改 `(task_id, step_id)` 复合键(与 HLS 同款工作目录)；detection 单源(砍 HLS keypoints JSON 的 inference_result，按帧 ts 对齐)。本文已据此重写。
>
> 相关：[inference/workflows/CLAUDE.md](../../app/services/inference/workflows/CLAUDE.md)（Workflow 作者文档）、[CLAUDE.md](../../CLAUDE.md)（项目总览，其中 `rt_processed`/`ca_ready` 描述与实现不一致，见下方「与旧文档的出入」）。

## 概述

- **写了什么**：把分层改造后**层与层之间真实的通信机制**对源码逐一核实并成文。核心结论是「每一层间的通信机制不同」——队列 / 共享缓冲 / 直接调用 / 落盘文件四种各司其职。
- **为什么写**：分层改造(L1 检测 / L2 特征聚合 / L3 时序产事实 / L4 规则出告警)落地后，缺一份和代码一致的数据流；本次更新进一步把 online/offline 分离后的链路(实时不落事实、离线另起独立 analyzer、落盘按 task+step)对齐到代码。
- **影响面**：纯文档。

## 分层数据流

```
WebSocket 入帧 (~30fps)
      │  ① 无锁 SPSC deque: ca_ready  (decoder 单生产 / dispatcher 单消费，靠 GIL 保原子)
      ▼
StageAwareDispatcher  (取帧 → 按 stage 分组)
      │  ② 有界 deque(maxlen=256)+Lock: _stage_queues[stage]
      ▼
MultiModelWorkerPool.infer_batch()                         ← L1 单帧检测(YOLO，bbox=特征)
      │  返回值 List[InferenceResult]（res.timestamp = 帧 ts）
      ▼
ModelWorkerService._write_back_results()        ←★ 实时/离线在此分叉(L2)：同一份 res.result 三路写出
      ├─③ 共享缓冲(交接点) push_detection → _slide_window[task]   (per-task deque，_slide_window_lock) ──→ 实时支
      ├─④ 原子快照        set_latest_inference                    (覆盖式，加锁)                        ──→ 可视化
      └─⑤ 落盘(L2)        FeatureStore.append(task_id, step_id, res) → {task}/{step}/features.jsonl
                                       (常开，缓冲批量写，best-effort；每行 {ts, features}，ts=帧 ts) ┄┄→ 离线支(★分叉首步)
                                              │ 推理线程(30fps)写 / 离线后续读，跨链路「落盘」交接
                                              ▼
ClientTemporalActor._tick()  (1Hz，100% 实时)
      │  get_slide_window(name) → window
      ├─⑥ 方法链(L3)  analyzer.run(window) = trans→infer→post_process → List[EventFact]
      │                      │ ⑦ 同 tick、同线程、直接值传递(facts 不进任何队列、不落盘)
      ├─        judge.step(facts) ─┘                                    ← L4 规则出告警
      ├─⑧ 原子快照   set_latest_temporal(events)
      └─⑨ 告警双路   try_pass_alarm_gate 闸门(5s 冷却) → persist_alarm(入告警队列→AlarmWorker 逐条异步 HTTP POST) + append_alarm_record(→alarm_log)
              (实时链路不落盘 EventFact：judge 判完即弃；事实账本是离线职责)

离线支(★从 ⑤ 分叉，本轮仅特征落盘就位、worker 待后续；online/offline 已彻底分离)：
      FeatureStore.load(task_id, step_id, source) ──→ 独立 OfflineAnalyzer.run(sequence) → SegmentFact
                                                  ──→ 离线 Judge → timeline，SegmentFact 写入 FactLedger({task}/{step}/facts.jsonl)
      注：离线是任务结束后的批处理，输入是 FeatureStore(特征)，与实时 ClientTemporalActor 零共享——
          不共享 analyzer 代码（实时 TemporalAnalyzer 已删 online/load/SegmentFact，离线另立 OfflineAnalyzer），只共享数据契约(DetectionOutput / Fact schema / FeatureStore 格式)。
          FactLedger 为 offline 专用账本——实时链路不写；**离线异步写、生命周期归离线 runner**，已从在线 manager 摘除（不由 set_task/stop 同步调度），类/契约休眠预留、待离线 worker 接入时自行 new+驱动。

VisualizationWorker._process_client()  (独立线程)
      │  读 ④get_latest_inference + get_latest_frame + ⑧get_latest_temporal → overlay 渲染
      ├─ append_ca_processed(frame) → ca_processed deque ─┐
      └─ set_latest_rendered(frame) → _latest_rendered 快照 │
                                                           │
   ┌── HLS 录制：drain_ca_processed() → persist_hls_segment(攒满 segment 长度)
   │      落 {task}/{step}/：processed_segment_*.mp4 + keypoints_*.json
   │      （keypoints JSON 每帧仅 {timestamp, keypoints}，不再含 inference_result——detection 单源于 FeatureStore）
   └── 实时推送：WS /ai/video 每 ~10ms 轮询 get_result → get_latest_rendered → base64 → send_text
                (「快照 + 前端轮询」模型，带时间戳去重 + 帧率控制；不是后端 push 队列)
```

## 数据通道（谁写 / 谁读 / 机制）

| # | 区间 | 机制 | 实体 | 写方 → 读方 |
|---|------|------|------|------|
| ① | WS入口 → Dispatcher | 无锁 SPSC deque | `ca_ready` [queues.py:96](../../app/services/client/queues.py#L96) | decoder → dispatcher |
| ② | Dispatcher → WorkerPool | 有界 deque + Lock | `_stage_queues[stage]` [dispatcher.py:57](../../app/services/inference/core/dispatcher.py#L57) | dispatcher → 推理线程 |
| ③ | **L1 → L3** | 共享缓冲(线程交接点) | `_slide_window[task]` per-task deque [queues.py:394](../../app/services/client/queues.py#L394) | 推理线程(30fps) → 时序线程(1Hz) |
| ④ | L1 → 可视化 | 原子快照(加锁覆盖) | `latest_inference` [service.py:299](../../app/services/inference/core/service.py#L299) | 推理线程 → VizWorker |
| ⑤ | **L2 落盘(★离线分叉首步)** | per-(task,step) JSONL(常开) | `FeatureStore` `{task}/{step}/features.jsonl` [service.py:306](../../app/services/inference/core/service.py#L306) | 推理线程 → 离线链路(load) |
| ⑥ | **L3 内部** | 方法链(进程内值) | `run()`=`trans→infer→post_process` [analyzer.py:62](../../app/services/inference/workflows/analyzer.py#L62) | analyzer 内部 |
| ⑦ | **L3 → L4** | 直接调用(facts 不进队列、不落盘) | `judge.step(analyzer.run(...))` [temporal.py:100](../../app/services/inference/workers/temporal.py#L100) | 同 tick 同线程，生产即消费 |
| ⑧ | L4 → 可视化 | 原子快照 | `latest_temporal` [queues.py:423](../../app/services/client/queues.py#L423) | 时序线程 → VizWorker |
| ⑨ | L4 → 持久化 | 闸门(5s 冷却) + 双写 | `persist_alarm` + `_alarm_log` [temporal.py:109](../../app/services/inference/workers/temporal.py#L109) | 时序线程 → 告警队列(AlarmWorker 逐条 HTTP POST) / 内存日志 |
| — | **L? → 账本(offline 专用)** | per-(task,step) JSONL | `FactLedger` `{task}/{step}/facts.jsonl` [store.py](../../app/services/inference/store.py) | **实时不写**；待离线 worker 写 SegmentFact |
| ⑩a | 可视化 → HLS | deque 攒批 → 落盘 | `ca_processed` → `persist_hls_segment` [queues.py:202](../../app/services/client/queues.py#L202) | VizWorker → HLS 录制 |
| ⑩b | 可视化 → 实时推送 | 快照 + 前端轮询 | `_latest_rendered` → WS [ai.py:83](../../app/routers/ai.py#L83) | VizWorker → 前端拉取 |

## 关键设计点

> **L1↔L3 是缓冲交接(③)，L3↔L4 是直接调用(⑦)——这是本质非对称。**
> L1(30fps)与 L3(1Hz)速度不同，必须用线程安全的 `_slide_window` 解耦；而 L3→L4 在同一 tick、同一线程里连续执行，既不挂队列也不挂缓冲，`facts` 是进程内值直接传，**不进 `ClientQueues`、不落盘**。

> **实时/离线在 L2 写回处分叉，分叉首步是 `FeatureStore.append`。** [`_write_back_results`](../../app/services/inference/core/service.py#L276) 把同一份 `res.result`(L1 出的 DetectionOutput)三路写出：`push_detection→slide_window`(内存交接，喂**实时** L3)、`set_latest_inference`(快照喂可视化)与 `FeatureStore.append→磁盘`(喂**离线** L3，日后经 `FeatureStore.load` 回读)。离线链路从 `FeatureStore.append` 起就独立了，**永不经过 ClientTemporalActor**——后者是 100% 实时。

> **online/offline 彻底分离，不共享时序行为。** 实时 `TemporalAnalyzer` 已收窄为 `run(window)->List[EventFact]`，删去了 `online` 参数 / `attach_feature_store` / `load` / `SegmentFact`。离线分段(`SegmentFact`)将由**另立的 `OfflineAnalyzer`**(任务结束后批处理 `run(sequence)`)承担，与实时只共享数据契约、不共享 trans/infer/post_process。基类暂不改名，待离线 worker 接入时再 rename。

> **实时链路不落盘事实；FactLedger 是 offline 专用账本。** `ClientTemporalActor._tick` 已删去 `FactLedger.append`——实时产出的 EventFact 经 `judge.step` 即变成告警(走 ⑨ 持久化)，事实本身判完即弃。`FactLedger` 类/契约休眠保留，但**已从在线 manager 摘除**（离线异步写不由在线 set_task/stop 调度），供日后离线 runner 自行 new+驱动写 `SegmentFact`(timeline)。

> **detection 单源 + 按帧 ts 对齐。** detection 只落 `FeatureStore`(features.jsonl)；HLS keypoints JSON 不再转储 `inference_result`(仅留 `{timestamp, keypoints}`)。每条特征记录的 `ts = res.timestamp = 帧捕获 ts`，与 HLS 段/keypoints 落盘所用的 `fd.timestamp` 同源同值，故离线/证据可按 `ts` 精确把特征行对回同帧。

> **落盘工作目录 `{task_id}/{step_id}/` 与 HLS 同款。** FeatureStore/FactLedger 改 `(task_id, step_id)` 复合键(`store.py._JsonlBuffer._path`)，与 `hls_strategy` 的 `db_dir/str(task_id)/str(step_id)` 同款；features/facts.jsonl 随该 step 目录被 HLS cleanup TTL 连带回收(修了旧版落在 task 根、TTL 永远扫不到的泄漏)。`ClientQueues.get_step_id()` 是统一的 step 解析口径。

> **实时推送是「快照 + 轮询」，非队列 push**：VizWorker 只覆盖 `_latest_rendered`，前端 WS 端循环每 ~10ms 拉一次、按时间戳去重 + 帧率控制。HLS 录制与实时推送是**两个独立消费者**，分别读 `ca_processed`(deque 攒批)与 `_latest_rendered`(快照)。

## 与旧文档的出入（待后续对齐）

| 处 | 旧 CLAUDE.md 描述 | 实际代码 |
|----|------|------|
| `ca_ready` | (隐含)异步队列 | 无锁 SPSC deque |
| `rt_processed` | 列为 4 条异步队列之一、承载实时推送 | **全仓不存在**；实时推送走 `_latest_rendered` 快照轮询 |

> 上述两点 CLAUDE.md / client 模块文档尚未对齐，本记录以源码为准。

## 验证

| 项 | 结果 |
|----|------|
| 数据流核对来源 | 逐文件读取 queues.py / dispatcher.py / service.py / temporal.py / analyzer.py / store.py / hls_strategy.py / ai.py |
| online/offline 分离 | `online=` / `_ledger.append` / keypoints `inference_result` 字段全仓清零；`TemporalAnalyzer` 仅 `run(window)->List[EventFact]` |
| 落盘键 | FeatureStore/FactLedger 走 `(task_id, step_id)`；`features.jsonl` 落 `{task}/{step}/` |
| 单测 | `pytest tests/` 全量 206 passed |
