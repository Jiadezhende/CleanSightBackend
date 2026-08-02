# Detection 链路压力日志 v0：CQ 与 dispatcher 的周期快照（只描述、不决策）

> **变更状态**：生效中（2026-07-27）
> **知识库**：已沉淀 → [DESIGN_OBSERVABILITY](../kb/DESIGN_OBSERVABILITY.md)（[PRESSURE] 周期快照）(2026-08-02)
>
> 承接：本次建立在 [20260727_DISPATCHER_INTERFACE_NARROW_LANDED.md](20260727_DISPATCHER_INTERFACE_NARROW_LANDED.md)（dispatcher↔proxy 收窄为 submit 布尔背压）之上——正是那次收窄让「下游拒收」变成了一个**完全静默**的布尔值。

## 概述

- **改了什么**：新增压力日志公共件 [`PressureReporter`](../../app/utils/pressure.py)，由 `ClientQueues`（三条 CA 队列）与 `StageAwareDispatcher`（stage deque）在任务运行时**周期性汇报**压力，形如 `[PRESSURE] component=… resource=… depth=… drop_delta=…`。
- **为什么改**：链路目前还没遇到压力，但压力一旦发生现在**看不见成因**——三条 CA 队列只有累计丢帧计数、无日志；proxy 拒收（`submit` 返 False）完全静默；旧的 `[INFER_PRESSURE]` 无帧年龄、无 delta 语义，且越权汇总了别人的 `ca_processed`。
- **影响面**：纯观测。不改任何入队/丢弃/提交/写回行为，不加 Prometheus 指标，不做降帧/限流/降级，不建中央控制器——这些留到后续（预案见 [20260727_DISPATCHER_ALLOC_BACKPRESSURE_PLAN.md](20260727_DISPATCHER_ALLOC_BACKPRESSURE_PLAN.md)）。

## 形态：周期快照，不是状态机

**到点（默认 10s）且当下有压力才打一行，平稳时完全静默。** 不做 ENTER/ONGOING/RECOVER 的边沿状态机——链路尚未遇到真实压力，先要的是一条能 grep 的心跳，而不是精确的压力窗口起止。

- **代价**：瞬时尖峰可能被采样点错过；压力结束表现为「不再出现新行」，没有恢复行。两条都接受。
- **收益**：整个机制就是「限频 + 谓词 + delta」三件事，[pressure.py](../../app/utils/pressure.py) 全文 ~180 行含注释。

## 谁汇报（每个资源恰好一个上报者）

| resource | 上报者 | 检查点 | 谓词 |
|---|---|---|---|
| `ca_ready` | `ClientQueues` | `append_ca_ready_with_throttle` 内部 | 深度 ≥ maxlen×0.5 |
| `ca_raw` | `ClientQueues` | `append_ca_raw` 内部 | 同上 |
| `ca_processed` | `ClientQueues` | `append_ca_processed` 内部 | 同上 |
| `stage_queue` | `StageAwareDispatcher` | 调度循环 1s 采样 | 同上 |

**只有这两个组件汇报。** 队列物理存于谁、检查就在谁的写入方法内部，由**写者线程顺带驱动**——不新起线程，也不把水位判定散到 decoder / VisualizationWorker 等生产者去（它们一行未改）。dispatcher 本来就有调度循环，1s 采一次即可（限频仍是 10s，采样密只换更及时的首行）。

> `ClientQueues` 的汇报天然「只在任务运行时」发生：没人写队列就没有 observe。`to_draining()` / `close()` 里静默 `reset()` 清计时与基线。

## 日志契约

```text
[PRESSURE] component=client_queues resource=ca_processed task_id=42 step_id=3 stage=CLEAN
depth=10 capacity=20 utilization=0.500 oldest_age_ms=4000 drop_total=0 drop_delta=0
reason=queue_high_watermark

[PRESSURE] component=dispatcher resource=stage_queue stage=CLEAN
depth=200 capacity=256 utilization=0.781 oldest_age_ms=10094
drop_total=0 drop_delta=0 reject_total=99 reject_delta=98 reason=queue_high_watermark
```

单行 key=value，全 WARNING；**不适用的字段直接不打**（不用 `-1` 等魔法值）。

**专用 logger `app.pressure`**（同既有 `app.startup_latency` 的做法）：所有压力行走同一个名字，
与业务日志解耦——嫌吵可一行关掉全部压力行、或单独路由到一个文件，而不影响 queues / dispatcher 自身的日志：

```python
logging.getLogger("app.pressure").setLevel(logging.ERROR)   # 全局静音压力行
```

调用方因此**不传 logger**（`PressureReporter(component, resource, identity=…)`）。

两条判定规则（都在公共件里，调用方不重复实现）：

1. **压力 = 调用方谓词 OR 任一 `*_total` 自上次报告后增长**。累计值只证明历史，delta 才证明当下；而且"丢完就空"时水位天然测不到，只有 delta 能报出来。
2. **首见播种**：首次见到某累计计数只记基线不报警，不把进程启动前的历史累计当成"刚刚丢的"。

`*_total` 字段自动配一个 `*_delta`，基线**只在实际打印后推进**（限频窗内的 observe 不会吃掉增量）。

## 改动详情

### 1. `app/services/client/queues.py` — 检查点在 `append_*` 内部

```python
with self._viz_lock:
    ...原有入队/丢帧逻辑不变...
    depth = len(self.ca_processed)
    drops = self.frames_dropped_processed
    # 队头 ts 只在越水位时多读一次，平稳期不付代价
    oldest_ts = self.ca_processed[0].timestamp if depth >= self._pressure_watermark else None
# 锁外上报（禁止持队列锁调 logger）
self._report_queue_pressure(self._processed_pressure, depth, oldest_ts, drops)
```

- 新增 `frames_dropped_ready` 计数（`ca_ready` 满拒帧），与 `frames_dropped_raw/processed` 同形；**只加计数，不加 Prometheus label**（入口丢帧已由 decoder 的 `ingress_backpressure` 承接，不重复计）。
- `ca_ready` 只在**放行 / 满拒**两条路径观测，抽帧跳过的帧不观测（省掉 (N−1)/N 的调用）。
- 锁清单新增一条：`*_pressure` 的内建锁是叶子锁，`append_*` 一律先出队列锁再上报，不与 6 把 payload 锁互嵌。

### 2. `app/services/inference/detection/dispatcher.py` — 规范化旧 `[INFER_PRESSURE]`

| | 旧 | 新 |
|---|---|---|
| 标记 | `[INFER_PRESSURE]` 一行塞 stage 段 + client 段 | `[PRESSURE] resource=stage_queue`，每 stage 一行 |
| 字段 | `q=depth/cap drop=n(+d)` | 补 `utilization` `oldest_age_ms` `reject_total/delta` |
| delta 基线 | 类内手工维护两个 dict | 交给 reporter |
| 采样 | 10s | 1s（限频仍 10s，日志量不变） |
| 越权 | 汇总各客户端 `ca_processed` | **删除**，交还 CQ 自报 |

**新增 `_stage_rejects`**：`submit()` 返 False 时计数并进压力行。proxy 内部的 `inflight` 是它的私有状态、不外泄，但**拒收这件事本身在提交侧看得见**——这样「下游满了发不出去」不再是个静默的布尔值，而 proxy 自己不需要任何日志代码。

`reject_delta>0` 就是「下游在拒收」的判据；没有 reject 而 deque 在涨，则是取帧快于提交。成因靠字段区分，不为每种成因再造一个 `reason`。

### 3. 保留项（刻意不动）

- **proxy / collector / 推理子进程**：不加任何压力日志。子进程死亡、wedge、写回失败是**事件**不是状态，照常直接打 ERROR/WARNING。
- decoder 的 `_should_drop_frame` 入口准入背压、`ingress_backpressure` 埋点、每 100 帧的 DEBUG——那是它自己的**准入决策**，与 CQ 报的**队列积压**语义不重叠。
- VisualizationWorker 的 `[VIZ_THROUGHPUT]`——量「速率亏空」，与积压正交。

## 数据通道

| 资源 | 谁写 | 谁读/排空 | 谁上报压力 |
|---|---|---|---|
| `ca_ready` | decoder | dispatcher | ClientQueues |
| `ca_raw` | decoder | HLSSegmentSweeper | ClientQueues |
| `ca_processed` | VisualizationWorker | HLSSegmentSweeper | ClientQueues |
| stage deque | dispatcher（取帧半） | dispatcher（提交半） | StageAwareDispatcher |
| inflight/pending | proxy.submit | proxy.collector | 不直接上报，由 dispatcher 的 `reject_total` 间接反映 |

## 日志量上界（结构性，非经验值）

```
最坏日志量 = reporter 个数 × (1 / interval)
reporter 个数 = 3 × 任务数 + active_stage 数
```

4 路流 + 2 个 stage = 14 个 reporter → 全链路全压时 ≤ 84 行/分钟，平稳时 **0 行**。
与帧率、采样率**完全解耦**：帧率翻倍、dispatcher 采样加密到 100ms，日志量都不变。

热路径开销实测（平稳路径，`observe` 走到谓词判定即 return）：

| 项 | 耗时 |
|---|---|
| `append_ca_processed`（含 observe） | 0.90 us/次 |
| `observe`（平稳、静默） | 0.62 us/次 |
| 4 路 30fps × 3 队列 = 360 次/s | 合计 0.22 ms/s CPU |

已核实的非放大路径：**重连不重建 CQ**（[service.py](../../app/services/stream/service.py) `restart_stream` 复用现有 `ClientQueues`），故重连风暴不会重置限频计时。只有切 step 建新 CQ 才重新计时，属低频人工操作。

## 后续（本期明确不做）

1. 自动降帧 / 限流 / 降级（`_admit_to_stage` 与 `_stage_backpressure` 接缝已就位，仍恒放行）。
2. Prometheus 指标与阈值告警。
3. persistence 的 `hls_queue` / `alarm_queue` 满目前是**每次丢都打一条 warning**，属同类问题，但不在本期范围。
4. 阈值调优：水位比 0.5、上报间隔 10s 都是模块常量，等有真实压力数据再谈上 settings。

## 验证

| 项 | 结果 |
|----|------|
| `tests/test_pressure_reporter.py`（限频 / delta / 首见播种 / 静默 / 异常不外泄） | 11 passed |
| `tests/test_cq_pressure_log.py`（含「日志不改行为」回归：返回值/丢帧计数/队列长度） | 9 passed |
| `tests/test_pipeline_drop_counters.py`（dispatcher 新契约 + 拒收计数进压力行 + 只报自己资源） | 7 passed |
| 全量 `pytest tests/` | 383 passed |
| 人造压力冒烟（`tmp/pressure_smoke.py`，不进仓库）：90 次 observe 出 3 行；11s 内 110 次采样只再出 1 行；`oldest_age_ms` 与 `reject_delta` 随时间增长；队列清空后彻底静默 | 通过 |
