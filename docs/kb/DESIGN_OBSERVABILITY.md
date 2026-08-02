> 更新时间：2026-08-02
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 可观测性：压力与吞吐诊断日志

后端在热路径上只打**仅压力时**的诊断日志：平稳完全静默，压力发生时才吐可 grep 的心跳。三条正交诊断线并存，均 try/except 包裹绝不影响热路径。当前定位是**纯观测**——不改任何入队/丢弃/提交/写回行为，不加 Prometheus 指标，不做降帧/限流/降级、不建中央控制器（这些属背压预案的后续，接缝已留、未接通）。

## 三条正交日志线

| 标记 | 量什么 | 谁打 | 语义 |
|------|--------|------|------|
| `[PRESSURE]` | 队列积压 / 丢帧 / 拒收 | `ClientQueues`（三条 CA 队列）+ `StageAwareDispatcher`（stage deque） | backlog：写者线程顺带驱动的周期快照 |
| `[VIZ_THROUGHPUT]` | 成帧速率亏空 | `VisualizationWorker` | throughput：真实成帧 fps / 空转占比 / 单帧渲染耗时，自动三侧归因 |
| `[BACKPRESSURE]` | 入口准入 / 录制队列 | `StreamService`、`FFmpegDecoder`（`stream/{service,decoder}.py`） | 准入决策 / persistence 队列满，与 `[PRESSURE]` 的队列积压语义不重叠 |

三者互不覆盖：`[VIZ_THROUGHPUT]` 量速率亏空、`[PRESSURE]` 量积压、`[BACKPRESSURE]` 量入口准入，正交。

## `[PRESSURE]`：周期快照，非状态机

公共件 [`PressureReporter`](../../app/utils/pressure.py)。**到点（默认 10s）且当下有压力才打一行，平稳静默**——不做 ENTER/ONGOING/RECOVER 边沿状态机，先要能 grep 的心跳，不要精确窗口起止。代价：瞬时尖峰可能被采样点错过、压力结束表现为「不再出新行」（无恢复行），两条都接受。整个机制 = **限频 + 谓词 + delta** 三件事。

### 每个资源恰好一个上报者

| resource | 上报者 | 检查点 | 谓词 |
|---|---|---|---|
| `ca_ready` | `ClientQueues` | `append_ca_ready_with_throttle` 内部 | 深度 ≥ `maxlen × 0.5` |
| `ca_raw` | `ClientQueues` | `append_ca_raw` 内部 | 同上 |
| `ca_processed` | `ClientQueues` | `append_ca_processed` 内部 | 同上 |
| `stage_queue` | `StageAwareDispatcher` | 调度循环 ~1s 采样 | 同上 |

只有这两个组件汇报：队列存于谁、检查就在谁的写入方法内部，**由写者线程顺带驱动**——不新起线程，不把水位判定散到 decoder / VisualizationWorker 等生产者（它们一行未改）。dispatcher 复用既有调度循环、每 ~1s 采一次（限频仍 10s，采样密只换更及时的首行）。`ClientQueues` 天然「只在任务运行时」汇报（没人写队列就没有 observe）；`to_draining()`/`close()` 里静默 `reset()` 清计时与基线。水位比 `DEFAULT_HIGH_WATERMARK_RATIO=0.5`、上报间隔 `DEFAULT_REPORT_INTERVAL=10.0` 均在 [pressure.py](../../app/utils/pressure.py)，等有真实压力数据再谈上 settings。

### 日志契约

单行 `key=value`，全 WARNING，走**专用 logger `app.pressure`**（同 `app.startup_latency`），与业务日志解耦——嫌吵可 `logging.getLogger("app.pressure").setLevel(logging.ERROR)` 一行全静音，不影响 queues/dispatcher 自身日志。调用方因此**不传 logger**。**不适用的字段直接不打**（不用 `-1` 魔法值）。

```
[PRESSURE] component=client_queues resource=ca_processed task_id=42 step_id=3 stage=CLEAN
depth=10 capacity=20 utilization=0.500 oldest_age_ms=4000 drop_total=0 drop_delta=0
reason=queue_high_watermark

[PRESSURE] component=dispatcher resource=stage_queue stage=CLEAN
depth=200 capacity=256 utilization=0.781 oldest_age_ms=10094
drop_total=0 drop_delta=0 reject_total=99 reject_delta=98 reason=counter_growth
```

### 两条判定规则（都在公共件里，调用方不重复实现）

1. **压力 = 调用方谓词 OR 任一 `*_total` 自上次报告后增长**。累计值只证明历史，delta 才证明当下；且「丢完就空」时水位天然测不到、只有 delta 能报出来。每个 `*_total` 自动配一个 `*_delta`。
2. **首见播种**：首次见到某累计计数只记基线不报警，不把进程启动前的历史累计当成「刚刚丢的」。基线**只在实际打印后推进**（限频窗内的 observe 不吃增量）。

### reason 的轴是「触发侧」，不是「成因」

`reason` 只有两个值（低基数），如实反映**这一行被哪一侧触发**：

- `REASON_QUEUE_HIGH_WATERMARK`（`queue_high_watermark`）：谓词越水位打的。
- `REASON_COUNTER_GROWTH`（`counter_growth`）：谓词没响、仅因 `*_total` 还在涨打的。

两者同时成立以**谓词为准**（水位是更强信号，计数增长由行内 `*_delta` 自明）。这条修正确保不再打出 `utilization=0.000 ... reason=queue_high_watermark` 这类自相矛盾行。**成因不进 reason**（仍由行内字段区分）：同为队列积压，`reject_delta>0` 说明下游在拒收、否则是取帧快于提交——「别为每种成因再造一个 reason」，触发侧与成因是不同的轴。

### 拒收：静默布尔值的可见化

dispatcher↔proxy 收窄为 `submit` 布尔背压后，「下游拒收」变成完全静默的布尔。dispatcher 的 `_stage_rejects` 在 `submit()` 返 False 时计数并进压力行——proxy 内部 `inflight` 是私有状态不外泄，但**拒收这件事在提交侧看得见**。proxy/collector/推理子进程**不加任何压力日志**（子进程死亡、wedge、写回失败是**事件**不是状态，照常直接 ERROR/WARNING）。

## `[VIZ_THROUGHPUT]`：成帧速率亏空的三侧归因

`VisualizationWorker` 量真实成帧 fps / 空转占比（`stale`）/ 单帧渲染耗时，自动三侧归因（优先级 `viz-starved > render-bound > supply-bound`）。三个基准解耦，勿混：

- **轮询率 vs 出帧率解耦**：VizWorker 轮询率抬到 `raw_fps`(30)、对 inference 流 2× 过采样（渲染仍按 `inference.ts` 去重，**每秒不同画面数恒 = inference_fps**，过采样只让新推理在更短 tick 内被抓到、消掉同频拍频的 stale 抖动，不增推理量）。故「速率亏空」基准是独立的 `output_fps`(= inference_fps) 而非轮询率——否则 `15 < 30×0.8` 恒真、100% 误报 supply-bound；render-bound 预算同理用出帧间隔 `1000/out_target` 而非 tick 间隔。日志打 `poll=..Hz` 区分二者。
- **viz-starved（worker 级 tick 健康度）**：`_tick_count`（遍历客户端**之前**自增，只反映线程转没转够、与客户端数无关）与标称轮询率比，< 80% 判 viz-starved——即单线程被 GIL 争用饿着。这是替代旧 `total >= expected_ticks*0.3` 门槛的正确信号：那道门槛拿 `total`（客户端有快照的 tick 数）当「run 存活时长」代理，viz 饿着时它同样塌 → 逻辑成环、把本该报出的故障咽掉。
- **出帧率分母改按 run 存活跨度**：`out_fps = rendered / span`，`span` 取该 run 窗内实测 `_first_seen→_last_seen`（+一个 tick_interval 补半开区间），两端都必须实测——窗末才起或窗中途 terminate 的 run 若拿窗界代替会把出帧率算低、误报 supply-bound。跨度 < `_MIN_SPAN_SEC`(1.0s) 只打数不判定。

仅补观测、不改行为：渲染去重仍在 `inference.ts`。**非缺陷留档**：viz 取 `cq.get_latest_frame()`（最新原始帧）叠检测框并盖 `inference.ts`，积压时框滞后于画面是**有意设计**（画面优先实时观感），别当 bug 修成按 ts 配对。

## 日志量上界（结构性，非经验值）

```
最坏日志量 = reporter 个数 × (1 / interval)
reporter 个数 = 3 × 任务数 + active_stage 数
```

4 路流 + 2 stage = 14 reporter → 全链路全压时 ≤ 84 行/分钟，平稳 **0 行**。与帧率、采样率**完全解耦**：帧率翻倍、采样加密到 100ms，日志量都不变。热路径开销实测平稳静默路径 `observe` ≈0.62 μs/次。重连不重建 CQ（复用现有 `ClientQueues`），重连风暴不重置限频计时。

## 边界与不重叠

- decoder 的 `_should_drop_frame` 入口准入背压 / `ingress_backpressure` 埋点是它自己的**准入决策**，与 CQ 报的**队列积压**语义不重叠。
- persistence 的 `hls_queue`/`alarm_queue` 满目前是**每次丢都打一条 warning**，属同类问题但不在 `[PRESSURE]` 体系内。
- `_admit_to_stage` / `_stage_backpressure` 接缝已就位但恒放行（自动降帧/限流/降级留后续）。

## 代码来源

- `app/utils/pressure.py`（`PressureReporter` + reason 常量 + logger `app.pressure`）
- `app/services/client/queues.py`（三条 CA 队列上报，`frames_dropped_ready/raw/processed`，`_pressure_watermark`）
- `app/services/inference/detection/dispatcher.py`（`_stage_drops`/`_stage_rejects`，stage deque 上报）
- `app/services/inference/visualization/worker.py`（`[VIZ_THROUGHPUT]`）
- `app/services/stream/{service,decoder}.py`（`[BACKPRESSURE]`）
- `tests/test_pressure_reporter.py`、`tests/test_cq_pressure_log.py`、`tests/test_pipeline_drop_counters.py`
