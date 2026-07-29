# [VIZ_THROUGHPUT] 拆出 tick 健康度 + 出帧率分母改按 run 存活跨度

> **变更状态**：生效中（2026-07-28）
> **知识库**：待沉淀 → kb/SERVICE_INFERENCE.md（[VIZ_THROUGHPUT] 一节，接 [20260726_VIZ_OVERSAMPLE_THROUGHPUT_BASIS.md](20260726_VIZ_OVERSAMPLE_THROUGHPUT_BASIS.md)）
> **相关**：issue #82（双路并发时 admin live fps 在 15↔3 跳变，掉帧位置待定位）

## 概述

- **改了什么**：
  1. 新增 **worker 级 tick 计数**（`_tick_count`），与标称轮询率比，低于 80% 判 **viz-starved**（新的第三侧归因）。
  2. `out_fps` 的分母从**固定窗长**改为**该 run 在窗内被观测到的跨度**（`_first_seen` → `_last_seen`）。
  3. **删除 `total >= expected_ticks * 0.3` 门槛**。
  4. 归因加优先级：`viz-starved > render-bound > supply-bound`。
  5. 日志行加 `ticks=N/expected`；非整窗存活的 run 额外标 `span=X.Xs`。
- **为什么改**：那道 0.3 门槛在排查 #82 时把唯一能定案的观测咽掉了，见下节。
- **影响面**：仅 `app/services/inference/visualization/worker.py` + `tests/test_viz_throughput_snapshot.py`。`_tick()`/`_process_client()` 新增 `now` 入参（无外部调用方）。**不动渲染热路径、不动 WS/HLS/Prometheus/写回**。

## 根因：0.3 门槛是分母 bug 的补丁，且代理量选错了

原判据 `total >= expected_ticks * 0.3`，注释写的是「滤空闲误报 / 判定是否真有推理流」。这个理由对不上代码：`_process_client` 在 `get_latest_inference()` 返回 None 时**直接 return、不计任何统计**，所以 `total` 里根本不含「流还没起来」的 tick，客户端在首个推理结果落地前压根不出现在统计字典里。

它真正在补的是下一行的分母口径：

```python
out_fps = rendered / window     # 分母是整个窗长，不是该 run 的存活时长
```

窗末尾才起来的 run，跑满 2s × 15fps = 30 帧，`out_fps` 被算成 30/10 = **3fps**，然后误判 supply-bound。0.3 门槛就是拿 `total` 当「存活时长」的代理来挡这个。

**但这个代理只在 tick 率标称时成立。** `total` 实际是「viz tick 到 **且** 看到该 run 有快照」的次数——viz 线程被 GIL 饿着时它同样会塌。于是逻辑成环：这个指标为了避免误报，预设了 viz 按 `poll_rate` 正常转，而**viz 转不动正是它本该报出来的故障**。

叠加上「`out_fps >= 0.8×out_target` 时也静默」，结果是「服务端完全健康」和「viz 严重饥饿」打出一模一样的（没有）日志——#82 卡在这里没法分叉。

## 改动详情

### 1. `_tick_count` — worker 级健康度，与客户端解耦

```python
def _tick(self, now: float):
    # tick 计数在遍历之前：即便本轮无客户端/全抛异常，这一 tick 也算跑过了
    self._tick_count += 1
    ...
```

计数放在遍历客户端**之前**，所以它只反映「这条线程有没有转够」，与有几个客户端、客户端活多久全无关（无客户端时循环照样空转到点）。判据：

```python
tick_rate = self._tick_count / window
tick_starved = poll_rate > 0 and tick_rate < poll_rate * _TICK_HEALTH_RATIO   # 0.8
```

### 2. `_first_seen` / `_last_seen` — 出帧率的分母区间

`_process_client` 在 None 门之后记首见（一次）与末见（每轮刷新）——有推理快照才算「这条流在供帧」：

```python
if task_id not in self._first_seen:
    self._first_seen[task_id] = now
self._last_seen[task_id] = now
```

窗末计算：

```python
span = min(window, max(0.0, last_seen - first_seen) + self.tick_interval)
out_fps = (rendered / span) if span > 0 else 0.0
```

**两端都必须取实测**：run 可能窗中途才起（首见晚），也可能窗中途就停（末见早），任一端拿窗界代替都会把出帧率算低。补一个 `tick_interval` 是把半开区间补回来（观测到 N 个 tick 对应的时长是 N×tick 而非 (N−1)×tick）。任一端缺失回退窗长，保留裸构造/直接喂计数的单测口径。

观测跨度 < `_MIN_SPAN_SEC`(1.0s) 的 run **只打数不下压力判定**——分母还没铺开，判了就是误报。

> 首版只记了 `_first_seen`、分母取「首见→窗末」，在第一次真实抓包里就打出两条假阳性：
> `16:01:42` terminate 掉的 task 119，在 `16:01:39→16:01:49` 这个窗里被算成
> `out=5.0fps (supply-bound)`——它其实活满 3s、跑满 15fps，只是分母被算成了 10s。
> 补 `_last_seen` 修正，回归见 `test_snapshot_silent_for_run_terminated_mid_window`。

### 3. 删门槛 + 归因优先级

0.3 门槛挡的误报被 (2) 从根上解掉，它误伤的场景被 (1) 接住，故整个删除。三侧归因按优先级排：

```python
if tick_starved:      tag = " (viz-starved)"
elif render_bound:    tag = " (render-bound)"
else:                 tag = " (supply-bound)"
```

tick 都不够时产出低是必然的，此时标 supply-bound 等于把账错记到上游。

### 4. 日志形状

```
[VIZ_THROUGHPUT] target=15fps poll=30Hz ticks=30/300 (viz-starved) render=2.0ms(max 5.0ms, budget 67ms) || 119 out=3.0fps stale=0% (viz-starved)
```

`ticks=N/expected` 一眼看出线程转没转够；`span=` 仅在非整窗存活时出现，免得 `out_fps` 被误读。

## 测试

`tests/test_viz_throughput_snapshot.py`：原 6 例补 `_tick_count`（以前隐含假设 tick 满额，现在显式写出），新增 3 例：

- `test_snapshot_flags_viz_starved` —— #82 场景回归：tick 塌到 3Hz，断言标 `viz-starved` 且**不得**出现 `supply-bound`。
- `test_snapshot_silent_for_just_started_run` —— 窗末 2s 才起的 run 跑满 15fps，不误报（分母左端）。
- `test_snapshot_silent_for_run_terminated_mid_window` —— 窗内第 3s 被 terminate 的 run 不误报（分母右端，用 16:01:49 那条真实日志的数字）。
- `test_snapshot_skips_verdict_for_too_short_span` —— 观测跨度 0.3s 只打数不判定。

原 `test_snapshot_silent_when_idle_stream` 被后两例替换：它测的是「tick 少所以别报」，而真实成因是「活得短所以分母算错」，现在按真实成因写。

## 语义边界（勿踩）

- `_tick_count` 是 **worker 级**信号，不要按客户端拆——它量的是线程调度，per-client 拆开就又回到「用客户端数据推断线程健康」的老路。
- `_MIN_SPAN_SEC` 不是 0.3 门槛的换皮：它只挡「样本时长不足以做速率判定」，判据里**不含 tick 数**，故不会随 viz 饥饿一起塌。
- `span` 的两端都是**实测**，不许拿窗界代替任何一端——首版只记左端就在真实日志里打出了假阳性（见上）。
- 这次只补观测、不改行为：渲染去重仍在 `inference.ts`，出帧恒 = inference_fps。
