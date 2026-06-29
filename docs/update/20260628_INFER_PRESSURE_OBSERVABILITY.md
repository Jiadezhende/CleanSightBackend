# 可观测性升级：推理链路静默丢帧（[INFER_PRESSURE]）+ 可视化吞吐量（[VIZ_THROUGHPUT]）

> **变更状态**：生效中（2026-06-28）
> **知识库**：待沉淀

## 概述

- **改了什么**：新增两条**正交**的诊断日志行，均**仅在有压力时打印**、平稳时静默以免刷屏：
  1. `[INFER_PRESSURE]`（量"积压/丢帧"，backlog）——把推理链路里此前**完全无计数**的两个静默丢帧点暴露出来：dispatcher 的 `_stage_queues` 满淘汰、`ClientQueues.ca_processed` 满淘汰；
  2. `[VIZ_THROUGHPUT]`（量"速率亏空"，throughput）——把 VizWorker 的**真实成帧 fps / 空转占比 / 单帧渲染耗时**量出来，自动判定 processed 成帧不足是 **supply-bound**（上游供帧慢）还是 **render-bound**（渲染慢）。

  两者与既有 `[BACKPRESSURE]` 行并存、职责分离。
- **为什么改**：现象是 **processed HLS 回放比 raw 快约 2x**。排查确认其根因是**时钟/速率失配**而非积压——processed 实际成帧率（实测 ~10fps）远低于写死的编码 fps（20），却因为帧根本没进队列/没被丢，`[BACKPRESSURE]`（只看 `ca_ready`/`ca_raw` 两个结构上几乎恒空的队列）和 backlog 类指标天然测不到。需要一条直接量"产出速率"的观测来定位亏空发生在哪一级。
- **影响面**：`app/services/inference/core/dispatcher.py`、`app/services/client/queues.py`、`app/services/inference/workers/visualization.py` 各加计数器/计时与访问器；不改 Prometheus / admin / health 端点；不替换 `[BACKPRESSURE]` 行。

> 注：本次为**纯观测**，未改播放快放本身。治本改法（按帧时间戳算有效 fps 编码 processed 段）待定，另行评估。

## 改动详情

### 1. `app/services/inference/core/dispatcher.py` — `_stage_queues` 丢帧计数 + 新日志行

- `__init__` 新增 `_stage_drops`（defaultdict，各 stage 满淘汰累计）、评估节流计数器 `_round_counter` / `_check_every_rounds`（随 `fetch_interval` 自适应，约 10s 评估一次）、`_pressure_queue_ratio`（积压前兆阈值）、`_last_logged_stage_drops` / `_last_logged_processed_drops`（算 delta 用）。
- `_fetch_and_dispatch_round` 入队前判满计数（满 = `append` 即将淘汰最旧帧），复用已持有的 `self._lock`：

```python
with self._lock:
    q = self._stage_queues[stage]
    if q.maxlen is not None and len(q) >= q.maxlen:
        self._stage_drops[stage] += 1
    q.append(req)
    ...
```

- `_dispatch_loop` 每轮 `_round_counter += 1`，到点调 `_log_pressure_snapshot()` 评估。
- 新增 `get_stage_drops()`（对称 `get_stage_queue_depths()`）与 `_log_pressure_snapshot()`，后者整体 try/except 包裹，日志失败绝不影响调度热路径。

**仅在有压力时才打印**，平稳时静默以免刷屏。判定为有压力 = 任一 stage 或 ca_processed 自上次报告以来有新增丢帧（delta>0），或任一 stage 队列深度 ≥ `maxlen * _pressure_queue_ratio`（积压前兆）。基线 `_last_logged_*` 仅在**实际打印后**推进，故 delta = 自上次报告以来的增量。

日志样例（仅压力时出现）：

```
[INFER_PRESSURE] stages: CLEAN q=180/256 drop=340(+25) | MOCK q=0/256 drop=0(+0) || clients: test.s112 ca_processed=5/2700 drop=0(+0)
```

> drop 同时给**累计值**（单调）与**自上次报告以来的 delta**——delta 才是"此刻是否在丢"的信号。

### 2. `app/services/client/queues.py` — ca_processed 丢帧计数 + 容量访问器

- 新增 `frames_dropped_processed`，在 `append_ca_processed` 满淘汰前 `+1`，复用 `_viz_lock`——与 `ca_raw` 的 `frames_dropped_raw`（queues.py:182-183）完全对称。
- 新增 `get_ca_processed_capacity()`（对称 `get_ca_raw_capacity()`）。
- 计数单调累计，**不在 `clear()` 重置**（同 `frames_dropped_raw`）。

### 3. `app/services/inference/workers/visualization.py` — 可视化吞吐量观测 + 去冗余拷贝

- `VisualizationWorker.__init__` 新增窗口统计：`_stat_rendered`（实际渲染帧数=processed 真实成帧率）、`_stat_stale`（有推理快照但无新结果而空转的 tick 数）、`_render_time_sum/_max/_calls`（渲染计时），以及评估窗 `_win_start` / `_eval_interval=10s`。
- `_process_client`：去重命中（`inference.timestamp <= last_ts`）时 `_stat_stale += 1`（**空转占比即"上游供帧慢"的直接信号**）；实际渲染段用 `time.perf_counter()` 计时并 `_stat_rendered += 1`。
- `run()` 每轮检查窗口是否到点（~10s），到点调 `_log_throughput_snapshot()` 后重置窗口。
- 新增 `_log_throughput_snapshot()`：**仅有压力时打印**——任一客户端有实际推理流（窗内 tick 数 ≥ 期望 30%，过滤近空闲流误报）且产出 < 目标 80%，或单帧渲染峰值 ≥ tick 预算。据"渲染峰值是否逼近预算"自动标 `render-bound` / `supply-bound`。整段 try/except 包裹，不影响渲染热路径。
- **顺带去冗余拷贝**：`_render` 原 `frame.copy()` 传入、`render()` 内部又 `copy` 一次 → 一帧两次整帧拷贝。删外层那次（render 全程只改副本、不动入参），省一次整帧 memcpy/帧。

日志样例（仅压力时出现）：

```
[VIZ_THROUGHPUT] target=20fps render=2.1ms(max 4.8ms, budget 50ms) || test.s112 out=10.2fps stale=49% (supply-bound)
```

> `out` = 真实成帧 fps（≈10 即坐实快放 2x）；`stale%` = 空转占比（高→供帧慢）；`render` avg/max 对比 `budget`（峰值逼近预算→render-bound）。

### 4. 保留项（不改动）

- 既有 `[BACKPRESSURE]` 行（`stream/service.py:584-586`）与 decoder 侧帧计数日志：职责清晰分离——`[BACKPRESSURE]`=入口/录制队列，`[INFER_PRESSURE]`=推理链路积压，`[VIZ_THROUGHPUT]`=可视化成帧速率。
- 未做：推理单槽 `_latest_inference` 覆盖计数、圆角框 ROI 局部化渲染优化（待 `[VIZ_THROUGHPUT]` 实测确认 render-bound 后再动）、Prometheus/admin 端点接入、processed 编码按真实 fps 治本（本次按需收敛范围）。

## 数据通道 / 行为说明

| 通道 | 填充 | 消费 | 满时行为 | 本次新增计数 |
|------|------|------|---------|------------|
| `ca_ready` | decoder（throttle≤inference_fps） | dispatcher（~100fps） | 拒入队 | 否（已有 `frames_dropped`） |
| `_stage_queues[stage]` | dispatcher | inference_loop | **静默淘汰最旧** | ✅ `_stage_drops`（[INFER_PRESSURE]） |
| `_latest_inference`（单槽） | inference | VizWorker（去重读） | 覆盖 latest-wins | ✅ 间接：VizWorker `_stat_stale`/`_stat_rendered`（[VIZ_THROUGHPUT]） |
| `ca_processed` | VizWorker | 持久化/drain | **静默淘汰最旧** | ✅ `frames_dropped_processed`（[INFER_PRESSURE]）+ 成帧 fps（[VIZ_THROUGHPUT]） |

## 验证

| 项 | 结果 |
|----|------|
| 新增单测 `tests/test_pipeline_drop_counters.py`（5 项：stage 满丢帧/未满不计/ca_processed 溢出/快照静默/有丢帧打印） | 5 passed |
| 新增单测 `tests/test_viz_throughput_snapshot.py`（4 项：健康静默/supply-bound/render-bound/近空闲不误报） | 4 passed |
| 全量 `pytest tests/` | 219 passed |
