# 可视化过采样轮询 + 吞吐告警基准与轮询率解耦

> **变更状态**：生效中（2026-07-26）
> **知识库**：待沉淀 → kb/SERVICE_INFERENCE.md（涉及 [VIZ_THROUGHPUT] 一节）

## 概述

- **改了什么**：
  1. VizWorker 的**轮询率**从 `settings.inference_fps`(15) 抬到 `settings.raw_fps`(30)，即对采样后的 inference 流做 **2× 过采样**；渲染仍按 `inference.ts` 去重，故**每秒吐出的不同画面数不变**（恒 = inference_fps）。
  2. `[VIZ_THROUGHPUT]` 告警的**速率亏空基准**从「轮询率」解耦成独立的**期望出帧率 `output_fps`（= inference_fps）**。
- **为什么改**：
  1. poll 率 == inference_fps 时，两个同频时钟拍频 → 部分 tick 读到旧的 `_latest_inference` 快照，记 `stale`；表现为抓帧有 33~66ms 抖动、`[VIZ_THROUGHPUT]` 恒报 **supply-bound**。过采样后每帧新推理都能在一个更短 tick 内被抓到，空转 tick 仅读单槽 + 比 ts（~µs），不增推理量。
  2. 但过采样直接暴露了告警的**基准 bug**：原判据 `out_fps < target*0.8` 里 `target = 1/tick_interval`（轮询率）。过采样后轮询率(30) > 出帧上限(15)，`15 < 30*0.8=24` **恒真** → 告警 100% 常亮且永远误标 supply-bound。根因是把「轮询率」当「期望出帧率」——二者仅在过采样前恰好相等。
- **影响面**：`app/services/inference/manager.py`（装配）、`app/services/inference/visualization/pool.py`（透传）、`app/services/inference/visualization/worker.py`（判据）；`tests/test_viz_throughput_snapshot.py` 加两回归。不动渲染热路径、不动 WS/HLS/Prometheus。

## 改动详情

### 1. `manager.py` — 轮询率与出帧基准分别注入

```python
self.visualization_pool = VisualizationWorkerPool(
    target_fps=settings.raw_fps,          # 轮询率：源视频帧率，对 inference 流 2× 过采样
    output_fps=settings.inference_fps,    # 期望出帧率：吞吐告警判速率亏空的基准（与轮询率解耦）
    stage_configs=None,
)
```

### 2. `visualization/pool.py` — 透传 `output_fps`

`VisualizationWorkerPool.__init__` 新增 `output_fps` 入参，`start()` 传给 `VisualizationWorker`。`target_fps` 仍只决定 `tick_interval = 1/target_fps`。

### 3. `visualization/worker.py` — 判据基准解耦

- `__init__` 新增 `output_fps`，缺省回退轮询率 `1/tick_interval`（兼容脱离装配的直接构造/旧测试，此时二者相等，退化为旧语义）。
- `_log_throughput_snapshot` 三个量各归其位：
  - **速率亏空**：`out_fps < out_target*0.8`，`out_target = output_fps`（期望出帧率）；
  - **是否真有流**（滤空闲误报）：`total >= expected_ticks*0.3`，`expected_ticks = poll_rate*window`（按轮询率算 tick 计数，正确）；
  - **render-bound**：`max_ms >= budget_ms`，`budget_ms = tick_interval*1000`（渲染须塞进一个 tick，正确）。
- 日志行加打 `poll=..Hz`，一眼分清轮询率 vs 出帧率。

日志样例（仅压力时出现）：

```
[VIZ_THROUGHPUT] target=15fps poll=30Hz render=2.0ms(max 5.0ms, budget 33ms) || test.s112 out=8.0fps stale=73% (supply-bound)
```

> `target` 是期望出帧率（15），`poll` 是轮询率（30）；`out_fps` 现在相对 **target** 判亏空，过采样带来的空转不再误报。

## 语义边界（勿踩）

- 这次**没有**把视频变成真 30fps 顺滑播放——去重门仍在 `inference.ts`，出帧恒 = inference_fps。真要 30fps 顺滑得改去重键为原始帧 ts、每来新原始帧就叠加当前最新推理（本次未做）。
- `output_fps` 缺省回退轮询率是**故意**的兼容路径，非疏漏：直接 `new VisualizationWorker(tick_interval=...)` 且不传 `output_fps` 时，二者相等，行为与过采样前一致。
