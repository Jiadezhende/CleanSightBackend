# 背压可观测性升级：暴露推理侧 `_stage_queues` 与 ca_processed 静默丢帧

> **变更状态**：生效中（2026-06-28）
> **知识库**：待沉淀

## 概述

- **改了什么**：新增一条 `[INFER_PRESSURE]` 推理压力日志行，把推理链路里此前**完全无计数**的两个静默丢帧点暴露出来——dispatcher 的 `_stage_queues` 满淘汰、`ClientQueues.ca_processed` 满淘汰；与既有 `[BACKPRESSURE]` 行并存、职责分离。
- **为什么改**：现有 `[BACKPRESSURE]` 行只打印 `ca_ready`/`ca_raw` 两个**结构上几乎恒空**的队列（入口被 throttle 卡死在 `inference_fps`，dispatcher ~100fps 抽干）。当推理 fps 掉速时，真实积压发生在下游 `_stage_queues`（`deque(maxlen=256)` 静默丢最旧帧），且 ca_processed 实际成帧率被压低导致录像快放——这些点一个计数器都没有，运维从日志完全看不出压力来源。
- **影响面**：`app/services/inference/core/dispatcher.py`、`app/services/client/queues.py` 各加计数器与访问器；不改 Prometheus / admin / health 端点；不替换 `[BACKPRESSURE]` 行。

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

### 3. 保留项（不改动）

- 既有 `[BACKPRESSURE]` 行（`stream/service.py:584-586`）与 decoder 侧帧计数日志：职责清晰分离——旧行=入口/录制队列，新行=推理链路压力。
- 未做：推理单槽 `_latest_inference` 覆盖计数、VizWorker 去重/有效 fps、Prometheus/admin 端点接入（本次按需收敛范围）。

## 数据通道 / 行为说明

| 通道 | 填充 | 消费 | 满时行为 | 本次新增计数 |
|------|------|------|---------|------------|
| `ca_ready` | decoder（throttle≤inference_fps） | dispatcher（~100fps） | 拒入队 | 否（已有 `frames_dropped`） |
| `_stage_queues[stage]` | dispatcher | inference_loop | **静默淘汰最旧** | ✅ `_stage_drops` |
| `ca_processed` | VizWorker | 持久化/drain | **静默淘汰最旧** | ✅ `frames_dropped_processed` |

## 验证

| 项 | 结果 |
|----|------|
| 新增单测 `tests/test_pipeline_drop_counters.py`（3 项：stage 满丢帧 / 未满不计 / ca_processed 溢出） | 3 passed |
| 相关回归（stage routing / stream / temporal / operator framework） | 33 passed |
| 全量 `pytest tests/` | 213 passed |
