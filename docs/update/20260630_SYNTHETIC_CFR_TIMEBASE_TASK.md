# Frame.timestamp：墙钟到达 → 合成 CFR 时钟

> **变更状态**：实施工单（2026-06-30）　<!-- 配套工单，待相位累加器落地后实施 -->
> **知识库**：待落地后沉淀
>
> 配套前置：[20260630_FRAME_DECIMATION_ACCUMULATOR_TASK.md](20260630_FRAME_DECIMATION_ACCUMULATOR_TASK.md)（抽帧改造；本工单解决"卡顿不破坏时序感知"，两步独立可绿）。
> 相关：[20260620_LAYERED_INFER_DATAFLOW.md](20260620_LAYERED_INFER_DATAFLOW.md)。

## 工单定位

把 [`FFmpegDecoder._process_frames`](../../app/services/stream/decoder.py) 给每帧打的 `Frame.timestamp` 从 **wall-clock 到达时刻** 换成 **合成 CFR 时钟**（`流起锚点 + frames_received/raw_fps`），让时序窗口在输入流卡顿/抖动时仍按真实媒体时间推进，不被网络 burst 塌缩。

## 现状根因

[`decoder.py`](../../app/services/stream/decoder.py) 现为每帧打 `timestamp = time.time()`（pipe 读出时刻）。网络卡顿后 ffmpeg 攒帧再 **burst** 吐出，decoder 背靠背读出，一串帧的时间戳挤在几十 ms 内。下游 L3 时序层**纯按时间戳裁窗**（[`operator.py:68`](../../app/services/inference/temporal/operator.py) `cutoff = ts[-1] - window_seconds`），于是把真实 0.5s 的视频压成 0.05s —— **时序感知被卡顿直接破坏**。

## 关键前提

- ffmpeg `-vf ...,fps=raw_fps` 输出 **CFR**：第 k 帧对应媒体时刻 `k/raw_fps`（相对滤镜起点），与 pipe 读出墙钟无关；
- 据此可由**帧计数**重建媒体时钟，无需从 rawvideo 管道取源 PTS（管道天然不携带 PTS）。

## 设计：合成 CFR 时钟 + 周期 re-anchor

```
# 流起或 re-anchor 时：
self._ts_anchor_wall = time.time()
self._ts_anchor_frame = self.frames_received
# 每帧：
timestamp = self._ts_anchor_wall + (self.frames_received - self._ts_anchor_frame) / self.raw_fps
```

- burst 到达的 N 帧拿到**正确间隔**（`1/raw_fps` 步进）的时间戳，窗口不塌缩；
- 卡顿期 ffmpeg 补的重复帧按真实时间推进 → 窗口看到"内容冻结、时间正常流逝"，对帧步时序模型是一致可学习的输入；
- **re-anchor**：ffmpeg 丢损坏帧（`discardcorrupt`）会使帧计数与真实时间缓慢错位；每隔 N 秒（或合成钟与 `time.time()` 偏差超阈值）重锚一次，吸收漂移。

## 爆炸半径（已核实，均正向或无影响）

- **告警绝对时间**：`detected_at` 用告警生成时自身 `time.time()`（[`persistence/models.py:66`](../../app/services/persistence/models.py)、[`alarm_strategy.py:67`](../../app/services/persistence/strategies/alarm_strategy.py)），**不读** Frame.timestamp → 无影响；
- **HLS eff_fps**：[`hls_strategy.py:55`](../../app/services/persistence/strategies/hls_strategy.py) 用时间戳 span 反推编码率。合成钟均匀 → eff_fps 精确 = inference_fps，**消除段间回放抖动**（正是 eff_fps 机制本想补偿的）→ 正向；
- **时序算子不变式**："同帧多流 ts 完全相等"（[`operator.py:82`](../../app/services/inference/temporal/operator.py)）—— 同帧计数 → 同合成 ts，**保持**。

## 范围

- [`decoder.py`](../../app/services/stream/decoder.py)：`_process_frames` 内 `Frame(timestamp=now, ...)` 改合成钟；增锚点字段与 re-anchor 逻辑；`raw_fps` 取自 `DecoderConfig.default_fps`（已有 = 30）。

## 不在本工单

- 真源 PTS 驱动（PyAV，方案 C）：仅当时序模型实测对"重复帧填充"敏感时才升级；先用本工单 + 实测说话。

## 验收

- 注入 burst（模拟卡顿后一次性吐 N 帧）：相邻 Frame.timestamp 间隔稳定 ≈ `1/raw_fps`，窗口内帧数与真实时长一致；
- 稳态：合成钟与 `time.time()` 偏差有界（re-anchor 生效）；
- HLS processed 段 eff_fps 落在 inference_fps±0.x，回放 1.0x。
