> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 可观测性：压力与吞吐诊断日志

热路径上只打**有压力时**的诊断日志：平稳完全静默，压力发生时才吐可 grep 的心跳。定位是纯观测——不改入队 / 丢弃 / 提交 / 写回行为，不加 Prometheus 指标，不做降帧 / 限流 / 降级，也不建中央控制器（接缝已留、未接通）。

## 三个日志标记各量一件事

| 标记 | 量什么 | 谁打 | 形态 |
|------|--------|------|------|
| `[PRESSURE]` | 队列积压 / 丢帧 / 拒收 | `ClientQueues`（三条 CA 像素队列）、`StageAwareDispatcher`（stage deque） | 写者线程顺带驱动的周期快照，WARNING，专用 logger `app.pressure` |
| `[VIZ_THROUGHPUT]` | 成帧速率亏空 | `VisualizationWorker` | 约 10 s 评估一次，有压力才打，自动三侧归因 |
| `[BACKPRESSURE]` | decoder 入口准入丢帧 | `FFmpegDecoder`（每丢 100 帧一条 DEBUG）；`StreamService.get_pending_count` 取不到 CQ 时的 WARNING | 准入决策，不是队列积压 |

录制两条落盘队列与 `alarm_queue` 满了是每丢一次打一条 warning（`[recording] 队列已满` / `[AlarmService] 告警队列已满`），属同类问题，但不在以上任何体系内。

## `[PRESSURE]`：周期快照，不做边沿状态机

公共件 [`PressureReporter`](../../app/services/utils/pressure.py)：**到点（默认 10 s）且此刻有压力才打一行**，平稳静默。整个机制就是「限频 + 谓词 + delta」。先要一条能 grep 的心跳，不要精确的压力窗口起止；代价是瞬时尖峰可能被采样点错过、压力结束表现为「不再出新行」，都接受。

### 每个资源恰好一个上报者，检查点在写入方法内部

| resource | 上报者 | 检查点 | 谓词 |
|----------|--------|--------|------|
| `ca_ready` / `ca_raw` / `ca_processed` | `ClientQueues` | 各自 `append_*` 内、队列锁外 | 深度 ≥ `maxlen × 0.5` |
| `stage_queue` | `StageAwareDispatcher` | 调度循环每 ~1 s 采样一次 | 同上 |

- 队列存在谁那里，就由谁在写入方法里上报；不另起线程，不把水位判定散到各生产者。`ClientQueues` 因此天然只在 run 运行时汇报，`to_draining()` / `close()` 静默 `reset()` 计时与基线。
- 水位比 0.5、间隔 10 s 是 `pressure.py` 常量，等有真实压力数据再谈上 settings。
- 只有状态型资源接它；子进程死亡、wedge、落盘失败是**事件**，照常直接打 ERROR / WARNING。
- **日志契约**：单行 `key=value`，全 WARNING，走专用 logger `app.pressure`（可一行 `setLevel(ERROR)` 静音，不影响业务日志）；不适用的字段直接不打，不用 `-1` 之类魔法值；`observe()` 整体 try/except，绝不影响热路径。

```text
[PRESSURE] component=dispatcher resource=stage_queue stage=CLEAN
depth=200 capacity=256 utilization=0.781 oldest_age_ms=10094
drop_total=0 drop_delta=0 reject_total=99 reject_delta=98 reason=counter_growth
```

### 判定规则都在公共件里，调用方不重复实现

1. **压力 = 调用方谓词 OR 任一 `*_total` 自上次打印后增长**。累计值只证明历史，delta 才证明当下；「丢完就空」时水位测不到，只有 delta 报得出来。每个 `*_total` 自动配一个 `*_delta`。
2. **首见播种**：第一次见到某个累计计数只记基线（delta=0），不把启动前的历史累计当成刚丢的。基线只在实际打印后推进，限频窗内的 observe 不吃增量。
3. **reason 标的是触发侧，不是成因**：只有两个值——谓词越水位打的是 `queue_high_watermark`，谓词没响、仅因计数增长打的是 `counter_growth`；两者同时成立以谓词为准。这保证不会出现 `utilization=0.000 ... reason=queue_high_watermark` 这种自相矛盾行。成因由行内字段区分（`reject_delta>0` 是下游拒收，否则是取帧快于提交），不为每种成因另造 reason。

### 拒收计数让静默的布尔背压可见

dispatcher → proxy 的背压是 `submit()` 返回的布尔值，本身完全静默。dispatcher 在 `submit()` 返回 False 时计 `_stage_rejects` 并带进压力行：proxy 内部 inflight 是私有状态不外泄，但拒收这件事在提交侧看得见。proxy / collector / 推理子进程不加压力日志。

## `[VIZ_THROUGHPUT]`：成帧亏空的三侧归因

`VisualizationWorker` 量真实出帧 fps、空转占比、单帧渲染耗时，按优先级 `viz-starved > render-bound > supply-bound` 归因。三个基准不能混：

- **速率亏空的基准是 `output_fps`（= inference_fps），不是轮询率**：轮询率是 `raw_fps`（30），对推理流 2× 过采样只为更快抓到新推理，渲染仍按 `inference.ts` 去重。拿轮询率作基准会让 `15 < 30×0.8` 恒真、100% 误报 supply-bound；render-bound 预算同理用出帧间隔 `1000/out_target`。
- **viz-starved 看 worker 级 tick 健康度**：`_tick_count` 在遍历客户端之前自增，低于标称轮询率的 80% 判 viz-starved（线程被 GIL 争用饿着）。反例：拿「有快照的 tick 数」当 run 存活时长的代理，viz 饿着时它同样塌，把本该报的故障咽掉。
- **出帧率分母用 run 的实测存活跨度** `_first_seen → _last_seen`（加一个 tick）；拿窗界代替会把中途起停的 run 出帧率算低、误报 supply-bound。跨度 < 1 s（`_MIN_SPAN_SEC`）只打数不判定。

**非缺陷**：viz 取最新原始帧叠检测框并盖 `inference.ts`，积压时框滞后于画面是有意设计（画面实时优先），不要修成按 ts 配对。

## 日志量上界是结构性的

```text
最坏日志量 = reporter 个数 × (1 / interval)
reporter 个数 = 3 × 任务数 + active stage 数
```

4 路流 + 2 个 stage = 14 个 reporter，全链路全压时 ≤ 84 行/分钟，平稳 0 行。与帧率、采样率无关：帧率翻倍或采样加密，日志量都不变。重连复用现有 CQ，重连风暴不会重置限频计时。

## `_admit_to_stage` / `_stage_backpressure` 接缝已留，恒放行

dispatcher 的入口降帧挂载点已就位但透明放行；自动降帧 / 限流 / 降级留待后续。

## 代码来源

- `app/services/utils/pressure.py`（`PressureReporter`、reason 常量、logger `app.pressure`）
- `app/services/client/queues.py`（三条 CA 队列上报、`_pressure_watermark`）
- `app/services/inference/online/detection/dispatcher.py`（`_stage_drops` / `_stage_rejects`、stage deque 上报、`_admit_to_stage`）
- `app/services/inference/online/visualization/visualization_worker.py`（`[VIZ_THROUGHPUT]`）
- `app/services/stream/{service,decoder}.py`（`[BACKPRESSURE]`）
- `tests/test_pressure_reporter.py`、`tests/test_cq_pressure_log.py`、`tests/test_pipeline_drop_counters.py`
