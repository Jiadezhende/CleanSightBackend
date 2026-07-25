# 配置层级定型：settings 真旋钮 / yaml 编排契约 / 衍生量（帧率·时间）

> **变更状态**：生效中（2026-07-23）　<!-- decimation 重构已落地，本篇把配置三层与"衍生量不进 yaml"不变式定型 -->
> **知识库**：待沉淀
>
> 承接：建立在 [20260722_FPS_TIME_VS_FRAME_DECOUPLE.md](20260722_FPS_TIME_VS_FRAME_DECOUPLE.md)（时间为唯一货币、fps 只在两点设定）之上；本篇不重述判据，只给"落到哪一层"的当前定盘与配置分层规则。真源均指向代码。

## 概述

- **改了什么**：把 fps/时间相关配置归成**三层**并定死边界——settings 级只放**真旋钮**，yaml 级只放**编排 + 契约**，其余全是**衍生量**（活在代码属性、由 settings 算出）。采样旋钮由 `inference_fps` 改为整数 `inference_decimation`，`inference_fps` 降为派生 property。
- **为什么改**：`inference_fps` 当过多处换算因子（上帝常量）；改造后必须有一份"谁在哪层、谁派生自谁"的定盘，否则衍生量迟早被人手滑写回 yaml、与 settings 漂移。
- **影响面**：[app/settings.py](../../app/settings.py)、四个 config loader（stream/persistence/client/inference）、抽帧器 [queues.py](../../app/services/client/queues.py)、HLS strategy、viz pool。

## 核心不变式（一句话判据）

| 层 | 放什么 | 判据 | 铁律 |
|----|--------|------|------|
| **settings 级** | 跨模块单一真源的**真旋钮** + **时间概念** | 能自由调、调了行为变、不与另一产物强绑 | 整数 fps 旋钮只有 2 个 |
| **yaml 级** | **编排**（选哪条 pipeline/哪个流）+ **契约**（随产物钉死的量） | 配错会崩（shape/key）或语义是"选择/契约" | 不含任何衍生量 |
| **衍生量** | settings 算出的换算结果（代码属性） | 必须与真源严格一致、不能独立设 | **永不进 yaml**——进了就是第二真源 → 漂移 |

## 一、settings 级（[app/settings.py](../../app/settings.py)）——真旋钮

跨模块单一真源，env 可覆盖（`CLEANSIGHT_*`）。这一层**只有两个整数 fps 旋钮 + 两个时间概念**：

| 字段 | 值/类型 | 角色 | env |
|------|---------|------|-----|
| `raw_fps` | 30 (int) | **生产者源**：解码 CFR 帧率 | `CLEANSIGHT_RAW_FPS` |
| `inference_decimation` | 2 (int) | **采样器唯一旋钮**：抽帧"每 N 帧留 1" | `CLEANSIGHT_INFERENCE_DECIMATION` |
| `ca_maxlen_seconds` | 90 (秒) | CA 缓存**时长**（时间货币，非帧数） | — |
| `ca_segment_seconds` | 10 (秒) | HLS 段**时长**（时间货币，非帧数） | — |

> 检测率 = `raw_fps / inference_decimation`。整数因子故**只能命中 raw_fps 的整除率**（30→15/10/7.5/6…，不支持 30→20 类非整除比）；非整除比的诉求由模型侧 `model_input_fps` 按 ts 重采样承接，不回退到这层放宽。

## 二、yaml 级（[config/*.yaml](../../config/)）——编排 + 契约，零衍生量

| 文件 | 留了什么 | 刻意不放 |
|------|---------|---------|
| [inference_config.yaml](../../config/inference_config.yaml) | `model_input_fps: 7.5`（**模型契约**，随产物钉死）、`window_seconds`（感受野） | 采样率/编码 fps |
| [stream_config.yaml](../../config/stream_config.yaml) | `resize`、`backpressure` 等解码参数 | `default_fps`（已删，见衍生量④） |
| [persistence_config.yaml](../../config/persistence_config.yaml) | `workers`、`queue_size`、`sweep_interval` | `segment_duration`、任何 fps（已删） |
| [client_config.yaml](../../config/client_config.yaml) | `resize_width/height` | 队列长、fps |

**关键状态：yaml 里"长得像 fps/时长"的量一个都不剩。** `model_input_fps` 是唯一的 fps，但它是契约不是旋钮——配错不崩、静默降级，故必填 + 加载期校验（见 [operator.py](../../app/services/inference/temporal/operator.py) `TemporalOperator.__init__`）。

> **天然护栏**：四个 config loader 都是裸 `**dict`、不做字段过滤。谁往 yaml 误写一个衍生量（如 `raw_fps: 25`），构造即 `TypeError` **当场崩**，而非静默生效——与"响亮地崩"哲学一致，不需额外校验。

## 三、衍生量（代码属性）——由 settings 算出，不可独立设

| # | 衍生量 | 位置 | 公式 | 值 | 消费者 |
|---|--------|------|------|----|--------|
| ① | `settings.inference_fps` | [settings.py](../../app/settings.py) property | `raw_fps / inference_decimation` | 15.0 (float) | viz 轮询率 |
| ② | `ClientConfig.ca_maxlen` | [client/config.py](../../app/services/client/config.py) | `ca_maxlen_seconds × raw_fps` | 2700 帧 | CQ 队列 |
| ③ | `ClientConfig.ca_segment_len` | [client/config.py](../../app/services/client/config.py) | `ca_segment_seconds × raw_fps` | 300 帧 | CQ 分段（**唯一真源**） |
| ④ | `DecoderConfig.default_fps` | [stream/config.py](../../app/services/stream/config.py) `default_factory` | `= settings.raw_fps` | 30 | ffmpeg `fps=` filter |
| ⑤ | `VizWorkerPool.target_fps` | [manager.py](../../app/services/inference/manager.py) 注入 | `= settings.inference_fps` | 15 | tick=1/15，渲染按 ts 去重 |
| ⑥ | `ClientQueues.inference_decimation` | [client/config.py](../../app/services/client/config.py) → cq_kwargs | 直读 `settings.inference_decimation` | 2 | 抽帧计数器 |

> ① 派生化后**无从被设成与 `raw_fps/N` 不一致的值**（旧代码它是独立字段、能漂），这正是本次收敛的意义。
> ⑤ 是"消费者继承源流速率"的合法派生——viz 消费的就是采样后 inference 流，poll 率跟随该流即可，非共用旋钮。

## 四、运行时反推（不是配置——从帧 ts 现算）

这些量既不在 settings 也不在 yaml，由数据现算，配置层完全不持：

- **HLS 段编码 `eff_fps`** = `(N-1) / span`，raw/processed 段逐段各自反推（[hls_strategy.py](../../app/services/persistence/strategies/hls_strategy.py) `_effective_fps`）。VideoWriter 与 EXTINF 同源 → 回放对齐墙钟。
- **WS 推帧率** = rendered 流实际到达率（"新帧即推"，按 `frame.timestamp` 去重，[ai.py](../../app/routers/ai.py)）。
- **模型入模密度** = `_resample_by_ts` 按 ts 重采样到 `model_input_fps`（[operator.py](../../app/services/inference/temporal/operator.py)）。

## 五、局部常量（模块私有——语义非管线 fps，故意不上升为配置）

| 常量 | 位置 | 语义 |
|------|------|------|
| `_WS_MAX_SEND_FPS = 30` | [ai.py](../../app/routers/ai.py) | WS 传输带宽上限（传输保护，不进部署配置） |
| `_DEGENERATE_FALLBACK_FPS = 15` | [hls_strategy.py](../../app/services/persistence/strategies/hls_strategy.py) | 退化段（单帧/span≤0/带外）兜底 EXTINF |
| `_EFF_FPS_MIN=1 / _EFF_FPS_MAX=60` | 同上 | eff_fps 反推合理带 |

> 这些与上游 fps 无关，就地取常量比引 settings 更诚实——它们回答的不是"流多快"，而是"传输别爆/退化段给个合理值"。

## 改动详情（本次相对 20260722）

1. **采样旋钮换根**：`settings.inference_fps`(15, 可设) → `inference_decimation`(2, 唯一旋钮)；`inference_fps` 改 property 派生。抽帧器 Bresenham 相位累加 → "每 N 帧留 1" 整数计数（CFR 已把时间烙成等距帧号，整数计数天然精确、无浮点累积）。见 [queues.py](../../app/services/client/queues.py) `append_ca_ready_with_throttle`。
2. **client 去 fps**：`ClientConfig`/`ClientQueues` 的 `inference_fps`/`raw_fps` 参数 → `inference_decimation`；删 `client.inference_fps ↔ settings` 冲突检查（现单一真源、无从冲突）。
3. **清死代码**：`InferenceManager` 的 `rt_fps`/`ca_segment_seconds` 参数 + `_ca_segment_len`（算出但全仓无人读）删除；[instance.py](../../app/services/inference/instance.py) 构造简化为 `InferenceManager()`。**CQ 段长单一真源明确落在衍生量③**，manager 的平行旧路消除。

## 验证

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | 335 passed |
| 抽帧率单测（整数计数） | 保留率精确 = 1/N |
| yaml→工厂装配 | `model_input_fps=7.5` 贯通；无衍生量落 yaml |
