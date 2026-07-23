# fps 解耦：时间为唯一货币，帧率只在「生产/采样」两点设定

> **变更状态**：生效中（2026-07-22）　<!-- 判据已随实现落地；结构解耦行为不变，Group 4 模型重采样改行为待 A/B 验。见文末「落地详情」 -->
> **知识库**：待沉淀
>
> 触发背景：排查「实时链路动作误分类」时，发现根因之一是训练 7.5fps / 推理 15fps 的 train/serve skew；顺藤摸出系统内 `inference_fps` 是个被多处误用的「上帝常量」。本篇把讨论收敛成一条可复用的评审判据。
> 承接：不依赖任何前序重构；相关代码引用均指向真源。

## 概述

- **提出什么**：一条判断「fps/帧率该不该在这里出现」的统一判据——**时间是子系统之间唯一的量纲货币；帧率只在把连续世界离散化的两个点（解码、检测采样）上才是真决策，其余一切消费者都由帧时间戳驱动，不设 fps。**
- **为什么提**：当前 `inference_fps` 被当作检测采样 / HLS processed 编码 / client 限流的共用常量，且时序模型输入节奏隐式等于它（15）而模型契约实为 7.5——一个数裹了多个正交决策，导致「动 fps 非无损」，且误检归因无法做隔离实验。
- **影响面**：不改行为，先立判据。落地时涉及 [app/settings.py](../../app/settings.py)、检测采样、HLS 持久化、AI WS 推流、时序算子输入适配。

## 病灶：一个 fps 冒充多条边界的换算因子

系统里本就存在**两个合法量纲域**，病根在两者交界处的换算被隐式化、全局化、且换错：

- **时间域**（有物理语义、跨子系统）：响应延迟、动作时长、缓存保留、回看窗口、告警门。都是关于真实世界的陈述，与 fps 无关。
- **帧域**（离散表示细节、子系统内部）：GRU 输入序列、逐帧检测、HLS 段的帧数编码。

**判据一句话**：有物理语义或跨子系统的量 → 必须用**秒**；只活在单个离散子系统内部的量 → 可用帧，但**进出该子系统必须过一道显式 fps 换算，且那个 fps 属于这条边界、不共享**。

### 六个症状归一

排查中出现的问题，在此判据下是同一个病：

| 表层症状 | 真身（时间 vs 帧域） |
|---------|--------------------|
| fps skew（训 7.5 / 推 15） | 模型帧域(7.5)被焊到检测帧域(15)，中间**无换算**——两种不同 fps 的帧被当同一种 |
| 缓存/段长会漂移 | 时间概念（90s）被**存成帧数**（`ca_maxlen=2700`），fps 一变即漂 |
| `inference_fps` 上帝常量 | 用**一个 fps** 去换算**多条各有各 fps 的边界** |
| 动 `inference_fps` 非无损 | 动一条边界的换算因子，连带改了其它几条边界 |
| tick 率是唯一解耦好的 | 因为它本就是**时间量纲**（`tick_interval` 单位秒），偏偏是唯一做对的 |
| 模型输入节奏是「契约」 | 它是模型帧域的私有 fps，属那条边界自己的换算常数，必须随产物走 |

## 核心判据：生产者/采样器 才 set，消费者只读时间戳

每一帧都带墙钟时间戳（pool 盖章、[hls_strategy `_effective_fps`](../../app/services/persistence/strategies/hls_strategy.py) 读它、clip_builder 用它）。**时间戳就是「何时」的唯一权威载体**，于是：

| 角色 | 是否设 fps | 系统里是谁 | 驱动 |
|------|:-:|-----------|------|
| **生产者**（把流强加到世界上） | ✅ set | 解码器 30（`raw_fps`） | 源/带宽 |
| **采样器**（改变流的密度） | ✅ set | 检测抽帧 30→15（`inference_fps`） | 感知/算力 |
| **消费者**（转码/推送/回放） | ❌ 读 ts | processed HLS、AI WS、raw HLS | 无——继承流 |
| **契约消费者**（有自己帧域的） | ❌ 读 ts + 重采样到契约 | 时序模型 → 7.5 | 产物契约 |

**真正需要「设 fps」的只剩两处：解码器(30)与检测采样(15)。** 其余全是消费者，全该由时间戳驱动。给消费者显式设 fps，结果只有两种：与流一致（冗余）或与流不一致（bug 源）。

### 系统内 fps 设定点现状

| # | 子系统 | 现状 | 目标角色 | 备注 |
|---|-------|------|---------|------|
| 1 | 解码器 | `raw_fps=30` 固定，ffmpeg `fps=` 强制 | 🟢 生产者，保留 set | [settings.py:93](../../app/settings.py#L93)、[stream/config.py:24](../../app/services/stream/config.py#L24) |
| 2 | 检测抽帧 | `inference_fps=15`（=raw×0.5） | 🟢 采样器，保留 set；**这是 `inference_fps` 唯一合法身份** | [settings.py:94](../../app/settings.py#L94) |
| 3 | 时序模型输入 | **无显式设定**，隐式=15；契约实为 7.5 | 🔴 契约消费者：读 ts 重采样到 7.5，随产物走 | skew 的精确坐标——契约根本没被表达出来 |
| 4 | 时序 tick | `tick_interval=0.5s`（2Hz，2026-07-23 由 1.0s 上调，降告警上升沿延迟） | ✅ 已对（时间域） | [actor.py:40](../../app/services/inference/temporal/actor.py#L40) |
| 5 | HLS raw 段 | `raw_fps=30` 假定固定 | 🟠 消费者：应读 ts（潜伏同类 bug，raw 较稳未咬到） | [hls_strategy.py:38](../../app/services/persistence/strategies/hls_strategy.py#L38) |
| 6 | HLS processed 段 | `eff_fps` 从 ts 反推 | ✅ 已对（时间域，正面样板） | [hls_strategy.py:592](../../app/services/persistence/strategies/hls_strategy.py#L592)；默认 `processed_fps=20` 已死、被覆盖 |
| 7 | AI WS 推流 | 硬编码 `1/30` 轮询 | 🟠 消费者：改「有新 rendered 帧就推」，rate 白捡、不再重发 | 推的是 [`get_latest_rendered()`](../../app/routers/ai.py#L122)，即 rendered 流 |
| 8 | 网关限流 | `rate_window=60s` 等 | ✅ 已对（时间域） | [settings.py:107](../../app/settings.py#L107) |

### 关键洞察

- **检测 / processed HLS / AI WS 不是三个 fps，是同一条 rendered 流的三个阶段**。收敛边界是「同一条流」，不是「都叫 fps」。正确统一是**共享来源 + 各自派生**（下游读流的实测速率），**不是共享一个字面量**——后者会重造小号上帝常量，并弄坏 processed 已修好的 `eff_fps` 抗漂移。
- **系统里其实只有两条源流**：raw 流@30（未标注，给 raw HLS 与检测采样）、rendered 流@检测实测率（~11–15，给 processed HLS 与 AI live view）。「几个 fps」塌缩为：**1 个自由旋钮（检测采样）+ 1 个契约（模型 7.5）+ 2 条源流，其余全派生。**
- **HLS processed 的 `_effective_fps` 是本课已学会的一半**：它因固定 fps 编码导致快放/抖动而改成「从墙钟反推」。修时序 skew、修 AI WS、修 HLS raw，都是**同一课在其它边界上再上一遍**。

## 解耦不变式（评审 checklist）

任何涉及帧率/时间的改动，逐条对照：

1. **一轴一驱动**：每个轴只由自己的驱动决定，改一个不牵动其余。
2. **契约钉死、随产物走**：模型输入节奏（7.5）来自产物元数据，不进部署配置；配错应在加载期以「契约不符」暴露，而非静默漂移。
3. **检测密度 ⟂ 模型节奏**：两者间必须隔一层显式重采样。
4. **时间概念用时间单位**：缓存/窗口/段长用秒，不用帧数。
5. **消费者不设 fps**：转码/推送/回放一律读帧时间戳；见到消费者上有 fps 常量，即为耦合点。
6. **裸 fps 常量被多处引用 = 危险信号**：多半在冒充多条边界的换算因子。

## 解耦解锁的调试实验

拆开耦合后，误检的每个假设才第一次可单独证伪（这是做本提案的初衷）：

| 假设 | 现在为何验不了 | 解耦后单独拧哪个轴 |
|------|--------------|-------------------|
| 提检测密度能压漏检 | 一动 fps，模型 skew + HLS 同时变，被污染 | 只拧检测采样 |
| fps skew 占误检多少 | skew 与检测密度绑死，分不开 | 只拧「模型节奏重采样」（对齐 7.5） |
| 降响应延迟是否够 | ——（已可单独验，tick 独立） | 只拧 tick 率 |
| 换因果模型净收益 | 前两项噪声未隔离，基线不干净 | 前三轴锁定后单换模型 |

## 落地详情（2026-07-22）

判据已随实现落地，一次性把三个 SET 点钉死、消费者改时间戳驱动、时间概念改秒。分组对照上表「系统内 fps 设定点现状」：

### 三个 SET 点（Group 1）

1. **生产者单一真源**：解码 CFR 从 `settings.raw_fps` 经 `default_factory` 取值（[stream/config.py `DecoderConfig.default_fps`](../../app/services/stream/config.py)），删除 [stream_config.yaml](../../config/stream_config.yaml) 里写死的 `default_fps: 30`——消除「两个巧合相等的独立 30」。
2. **`inference_fps` 收敛为唯一采样旋钮**：抽帧器（[queues.py Bresenham](../../app/services/client/queues.py)）不动，[settings.py:94](../../app/settings.py#L94) 注释重写为「检测抽帧采样率——系统唯一 fps 旋钮」，删「HLS processed 打标 / client 限流共用」。收敛后 `settings.inference_fps` 的功能消费者只剩两个合法项：**采样器本尊**（queues 抽帧）与 **viz poll 率**（[manager.py](../../app/services/inference/manager.py)→pool→worker，消费者继承采样流速率、渲染按 `inference.ts` 去重）。HLS processed 的 `processed_fps` 兜底原来也借用 `inference_fps`，一并断开——改读 [persistence/config.py `_PROCESSED_FPS_FALLBACK`](../../app/services/persistence/config.py) 通用编码常量（processed 段主用 `eff_fps` 从 ts 反推，此常量仅退化段兜底，与采样率无关）。
3. **模型契约在 inference 侧声明**：`model_input_fps: 7.5` 落 [inference_config.yaml](../../config/inference_config.yaml) CleanOperator `params`（紧挨 `actions`/`objects`）。**决策修正**：原计划 4「下沉到产物元数据」被否——7.5 配错**不崩、静默降级**（≠ shape/key mismatch），按判据属「真配置」而非「契约副本」，故声明在 inference 配置、不动跨仓产物 schema。加载期 `None`/`≤0` 在 [TemporalOperator.__init__](../../app/services/inference/temporal/operator.py) 暴露信号。

### 消费者去 fps 化（Group 2）

4. **可视化 worker**：确认 15fps poll 是「消费者继承源流速率」的合法派生（渲染按 `inference.ts` 去重、组批推理产出即 15fps），**非上帝常量**，仅 [manager.py](../../app/services/inference/manager.py) 注释澄清、不改代码。
5. **AI WS**：删 [ai.py](../../app/routers/ai.py) 的 `frame_interval=1.0/30` 伪 fps floor，主驱动改「新帧即推」（`frame.timestamp` 去重）；带宽上限保留为**模块内传输层常量** `_WS_MAX_SEND_FPS=30`（语义=传输节流、非管线 fps，**不进部署配置**——传输保护无需暴露成可调旋钮）。
6. **HLS raw 段**：[hls_strategy.py `_persist_raw_segment`](../../app/services/persistence/strategies/hls_strategy.py) 的 VideoWriter + EXTINF 改用 `_effective_fps(frames, raw_fps)`（与 processed 段同款），`raw_fps` 降为 fallback。
7. **clip_builder**：[clip_builder.py `_run_ffmpeg`](../../app/services/lab/clip_builder.py) 的 concat EXTINF 从固定 10s 改**逐段 ts 跨度**（末段用中位估算），消除 fps 漂移下 `-ss` seek 累计错位；窗口已过 `_validate_continuity` 保证相邻段连续。

### 时间概念改秒（Group 3）

8. `settings.ca_maxlen`/`ca_segment_len`（帧数）→ `ca_maxlen_seconds=90`/`ca_segment_seconds=10`（秒）；帧数在 [client/config.py](../../app/services/client/config.py) 属性按 `raw_fps` 显式换算（值仍 2700/300，**零行为变化**），删 [instance.py](../../app/services/inference/instance.py) 的帧→秒→帧往返。

### 模型重采样（Group 4，唯一改行为）

9. [TemporalOperator._resample_by_ts](../../app/services/inference/temporal/operator.py) + [CleanOperator._advance](../../app/services/inference/workflows/clean.py)：入模前按 `f.ts` **相位网格抽稀**到 `model_input_fps`（15fps 10s 窗口 150→75 帧；网格前进不累积漂移、遇缺口重锚不追补突发）。新帧门/`last_ts` 推进仍基于完整窗口，重采样只定喂模型的时间轴密度。

**验证**：`pytest tests/` 335 passed；YAML→工厂装配确认 `model_input_fps=7.5` 贯通；结构解耦（1–3、5–8）行为不变。

### 保留后续

- **Group 4 单独 A/B 验**：取已知动作 clip，对比重采样前/后分类输出，量 train/serve skew 对误分类的占比（这是本次唯一改分类结果的改动）。
- **新解锁实验**：解耦后可只拧检测采样率而不动模型节奏——降 `inference_fps` 省 GPU 等实验现可单独证伪（见上表「解耦解锁的调试实验」）。

> 关联但正交的另一条线：实时误分类的架构性根因是**在线跑双向 BiGRU 并读最后时间步**（后向上下文为零），需换因果单向 GRU、BiGRU 冻为离线参考。那属模型侧改造，不在本篇 fps 解耦范围，另立记录。
