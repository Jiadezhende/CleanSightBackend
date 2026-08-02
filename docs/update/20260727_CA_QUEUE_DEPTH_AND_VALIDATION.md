# CA 队列缓存 90s→30s + 配置校验改关系式（去绝对帧数魔数）

> **变更状态**：生效中（2026-07-27）。仅调容量默认值与配置校验口径，无行为/契约改动，全套 355 passed。
> **知识库**：已沉淀 → [SERVICE_CLIENT_STATE](../kb/SERVICE_CLIENT_STATE.md)（CA 队列容量默认值 + 关系式校验）(2026-08-02)

## 概述

- **改了什么**：两件事——(1) `settings.ca_maxlen_seconds` 90→30，把三条 CA 队列的深度天花板压掉 2/3；(2) `ClientConfig._validate_config` 删掉绝对帧数地板，换成两条关系式不变式。
- **为什么改**：90s×30fps=2700 帧的天花板在多路场景下是纯内存风险（10 路聚合上限 ~74GB），而深缓冲对推理腿本就有害；校验里的 `ca_maxlen < 300` 是秒制重构的遗留魔数，语义会随 `raw_fps` 漂移。
- **影响面**：`app/settings.py`、`app/services/client/{config,queues}.py`。稳态行为不变（稳态下三队列本就很浅）；仅在消费端持续停滞时更早触顶丢帧。

## 1. 容量：`ca_maxlen_seconds` 90 → 30

`ca_maxlen` 不是可直接填的帧数，而是**派生**的（[20260722_FPS_TIME_VS_FRAME_DECOUPLE.md](20260722_FPS_TIME_VS_FRAME_DECOUPLE.md) 立的「时间为跨子系统货币」）：

```
settings.ca_maxlen_seconds × settings.raw_fps  →  ClientConfig.ca_maxlen（属性换算）
  → cq_kwargs() → ClientQueues(ca_maxlen=…) → ca_ready / ca_raw / ca_processed 三个 deque
```

内存天花板（640×480 BGR ≈ 0.92MB/帧 × 3 队列）：

| ca_maxlen_seconds | 帧数 | 每 client 上限 | 10 路聚合 | HLS 段余量 |
|---|---|---|---|---|
| 90（旧） | 2700 | 7.42 GB | 74.2 GB | 9.0× |
| **30（新）** | **900** | **2.32 GB** | **23.2 GB** | **3.0×** |

取 30s 的依据：
- **推理腿（`ca_ready`）本就不需要深缓冲**。dispatcher 以约 1000fps 掏帧、输入仅约 250fps（10 路×25），该队列恒被掏空；真填满 90s 反而意味着在推理 90 秒前的陈帧，对实时督促无价值。这条腿的天花板只是兜底，不是工作集。
- **录制腿（`ca_raw`）才是要余量的一侧**：丢帧 = 录像永久空洞（非仅延迟）。30s = 3 个 HLS 段（`ca_segment_seconds`=10s），够吸收分段消费抖动。

> **未实测的假设**：3 段余量基于「ffmpeg/segmenter 停顿 < 30s」。若上线后出现 `raw_backpressure` 丢帧，即为该假设被证伪的信号，回调秒数即可（改一个数）。

同步 `ClientQueues.__init__` 的裸建默认 `ca_maxlen` 2700→900，并在 docstring 点明「生产值由 settings 派生，此默认仅裸建/测试兜底」——它是第二真源，生产路径恒被 `cq_kwargs()` 覆盖，此前两处不一致易被误当旋钮。

## 2. 校验：绝对帧数 → 关系式

**删** `if self.ca_maxlen < 300`。它是秒制重构前的遗留：那时 `ca_maxlen` 是字面量 2700 帧，手挑 300 当「30fps 下至少 10 秒」的地板尚且成立；改派生后这个数冻在帧空间里，**语义随一个无关旋钮漂移**——30fps 下意味 10s、15fps 下意味 20s。且它与 `ca_segment_len`（10s×30=300）数值相等纯属当前配置的巧合，易被误读为两者有关联。

**换成两条关系式**（阈值提成具名常量，文案里的百分比由比率算出，不再手写第二处数字）：

| # | 判据 | 级别 | 理由 |
|---|---|---|---|
| 1 | `ca_segment_seconds < _MIN_SEGMENT_SECONDS`(5.0)，**按秒判** | ⚠️ | 段过短 → 段数与每段 ffmpeg 固定开销放大；段长是时间概念，与 raw_fps 无关 |
| 2 | `ca_segment_len > ca_maxlen` | ❌ | 装不下一个段 → 永远触发不了分段（数学必然，致命） |
| 3 | `ca_maxlen < ca_segment_len × _SEGMENT_HEADROOM_RATIO`(1.2) | ⚠️ | 装得下但无 20% 余量 → 分段消费一抖动就丢帧 → 录像空洞 |

2 与 3 用 `if/elif`：装不下时只报致命那条，不叠加余量告警刷屏。三个分支均按边界构造验证过（1s 段触发 #1、28s 段=1.07× 触发 #3、40s 段触发 #2）；当前配置（段 10s、余量 3.00×）零告警。

> `_MIN_SEGMENT_SECONDS` / `_SEGMENT_HEADROOM_RATIO` 本身仍是经验值，只是从散落魔数收敛成具名、带理由、单点可调的旋钮——不同于 #2 那条有数学必然性支撑。
