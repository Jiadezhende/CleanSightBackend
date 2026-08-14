# 离线特征融合实验：raw 帧索引反查、多 recipe 特征导出器、实验平台时间线

> **变更状态**：提案（2026-08-14）　<!-- 需求已定稿，技术方案与实现另出；尚未改动任何代码 -->
> **知识库**：待沉淀
>
> 承接：[CLEAN 清洗阶段模型提案](20260814_CLEAN_STAGE_MODEL_PROPOSAL.md) 的第一阶段。那一篇给的是业务识别目标与模型候选的全景；**本文只取其中「动作时间线」一条**，把它切成现在就能开工的工程需求，并把该提案 §3.4 悬而未决的「raw 图像与检测特征对齐」定死为方案 S1。清水/有色液体、操作部悬空两个专项本期完全不碰。

## 概述

- **改了什么**：三块。① raw 段落盘时补一份**逐帧索引 sidecar**，让 `features.jsonl` 的 `ts` 能无歧义定位到具体像素帧；② 新增**离线融合特征导出器**，直接产出可喂入模型的输入，实现 R0/R1/R2 三个待验证 recipe；③ lab 页面重命名为**实验平台**，新增离线推理触发与清洗时间线可视化。
- **为什么改**：当前离线链路只有 bbox 派生特征（`clean_bbox_v2_top1_impute`），刷具检不到时动作证据就断了。要验证「加视觉特征是否真有增益」，前提是能把 RGB 帧和检测框对齐到同一帧——而这条现在不成立。
- **影响面**：[hls_strategy.py](../../app/services/persistence/strategies/hls_strategy.py) 落盘路径（**只增写一个 sidecar 文件，不动视频数据与 playlist**）、新增离线导出模块、[lab.py](../../app/routers/lab.py) 与 [static/lab/index.html](../../app/static/lab/index.html)。**不改**在线推理链路、**不改** `features.jsonl` 契约、**不改**动作标签集。

## 一、边界：本期做什么、不做什么

| 决策 | 结论 | 依据 |
|------|------|------|
| 识别目标 | **只做动作识别**。清水/有色液体、操作部悬空均不做 | 一次只验一个变量；颜色专项与动作主模型无共享结论，混做只会让「视觉特征有没有用」这个问题失焦 |
| 动作标签集 | **冻在现有 6 类**（`idle` / `long_brush_insert` / `long_brush_withdraw` / `short_brush_cleaning` / `flush` / `air_injection`），见 [clean.py `ACTION_LABELS`](../../app/services/inference/offline/impl/clean.py) | 扩成提案的 8 类（加 `wipe`/`drain`）会让现有三个 checkpoint 全部失效，bbox-only 基线要重训才能做对照；而 `wipe`/`drain` 现在也没有标注数据 |
| 检测来源 | **绝不离线重跑 YOLO**，一律复用在线落盘的 `features.jsonl` | 在线跑 YOLO 的价值就是「分析结果更快产出 + 画面上能直观看到推理在跑」。离线重跑一遍，实时链路就没有存在意义了 |
| 离线要跑的模型 | **只有视觉 backbone**（共享 CNN）。它在线上不存在，只能离线跑 | 与上一条不矛盾：不重复线上已有的，只补线上没有的 |
| 完整 Segmenter | **本期不写** | 融合模型还没训出来，推理侧没有 checkpoint 可吃。先把「造训练输入」和「看结果」打通，实验有结论后再写 |
| 清洗池 polygon / `f_basin` | **不做** | 池区特征主要服务清水判断与悬空专项，两者本期都不做；动作主干本就不依赖它（提案 §3.2B） |
| 标注真值导入与对照 | **不做**，留下一期 | 本期时间线只展示模型预测，供人工肉眼核对切分是否合理。上 segmental F1/Edit 需要先把 Label Studio 的区间标注读回来，是独立一块 |

## 二、raw 帧反查：方案 S1（逐帧索引 sidecar）

### 2.1 现状与问题

**`features.jsonl` 的 `ts` 与 raw 帧 ts 是严格同源同值的**——这条已经是硬不变式（[HLS 墙钟时间轴需求](20260813_HLS_WALLCLOCK_TIMELINE_REQUIREMENTS.md) I8），不需要额外建立。

缺的不是时间对不对得上，是**段内定位**：

- 段文件名带段首 ts：`raw_segment_{int(start_ts * 1e6)}.mp4`，所以「ts → 哪个段」是准的；
- 但段内 N 帧是用 `eff_fps = (N-1)/span` 反推出的**合成 CFR** 写进 mp4 的（[_persist_raw_segment](../../app/services/persistence/strategies/hls_strategy.py#L496)），**逐帧真实 ts 写完即丢**；
- 于是「段内第几帧」只能按平均帧率反推，源侧抖动会在段内累积成偏差。

### 2.2 方案

落段时同步写一份该段的帧 ts 有序表。反查即：`ts` → 定位段 → 在有序表里二分得 `ordinal` → 解码该段取第 `ordinal` 帧。合成 CFR 下 `ordinal ↔ PTS` 是恒等关系，所以**这条链路精确、无近似**。

**明确不做 S2（按 `eff_fps` 反推 ordinal）**：它零改动、且对历史数据也能用，但会带来 ±0.1~0.2s 的帧偏差。这种偏差会悄悄进训练集，日后模型边界误差究竟是特征不行还是对齐不准，将无法分辨。

**sidecar 契约**：

- 位置与命名：与段同目录同名，`raw_segment_{ts_us}.idx.json`（一段一文件，随段被 TTL 一起回收，无独立生命周期）；
- 内容：**只有按写入顺序排列的逐帧源 ts 列表**（长度 == 该段实际写入帧数）。**刻意不记** `eff_fps`、段时长、timescale；
- 写入时机：`_persist_raw_segment` 内、`VideoWriter.release()` 之后，**在目录锁之外**；tmp + `os.replace` 原子替换；
- 失败策略：**best-effort，写失败只告警不抛**——落盘主链路的可用性优先于实验数据完整性（与 [FeatureStore](../../app/services/inference/feature/store.py) 同款取舍）。

> **与在制 HLS 改造的隔离**（[时基修复](20260813_HLS_SEGMENT_TIMESCALE_FIX.md) / [墙钟时间轴](20260813_HLS_WALLCLOCK_TIMELINE_REQUIREMENTS.md)）。两篇都要重写 `_persist_raw_segment`，本方案按三条规避冲突：
>
> 1. **不进目录锁。** 锁内那三段（transcode + playlist append + metadata）正是被改造的部分；sidecar 是段私有文件，与 playlist/metadata 无竞争、不参与 tfdt 累计，没有进锁的理由。
> 2. **不记任何容器层参数。** `eff_fps`、段时长、timescale 全在被改之列，记下来就是第二份会漂移的真源。sidecar 只回答一个问题：**哪些帧、按什么顺序进了这个段**——这个语义在两篇改造下都不变。
> 3. **取帧靠顺序解码数第 i 帧，不靠 PTS/`eff_fps` 反算。** 于是段内是合成 CFR 还是真实 PTS、时基怎么修，映射都成立。封段触发改造（I11 断流封段）同样无影响——封段怎么切，sidecar 就怎么记。
>
> 实现上落成**独立模块 + `hls_strategy` 里一行调用**，把 merge 冲突面压到一行。

**边界情况**（每条都要在实现里显式处置，不能默认「不会发生」）：

| 情况 | 处置 |
|------|------|
| 转码 fMP4 失败（段不进 playlist） | sidecar 照常写。反查侧须校验段是否在 playlist 中；不在则该区间标记为**不可取帧**，而非静默错位 |
| 未来的黑屏段（[墙钟需求](20260813_HLS_WALLCLOCK_TIMELINE_REQUIREMENTS.md) 落地后） | 合成产物，无源帧，**不写 sidecar**。反查侧「找不到 sidecar」= 该区间无像素证据 |
| 帧在入段前已被队列淘汰 | sidecar 记的是**实际落盘的帧**。该 ts 在 feature 里有、在 sidecar 里没有 → 显式 miss，导出时计入丢帧统计并 mask 掉，不做最近邻凑数 |
| 同 (task, step) 重启 supersede | 段文件本身已由现有 `purge_step_dir` / 段命名机制处理，sidecar 与段同名同目录，天然同生共死 |
| 单段解码 | raw 段是 fMP4 fragment（无 moov），**不能单独解码**。反查须像 [ClipBuilder](../../app/services/lab/clip_builder.py) 那样写临时 m3u8（`EXT-X-MAP` 引 `init.mp4`）喂 HLS demuxer |

### 2.3 直接后果：存量数据不进融合实验

S1 只对改动落地后新产生的数据有效。**sidecar 之前录的所有 step 都没有精确帧对齐。存量一律不做迁移、不做修复，直接不用。**（它们仍可用于 bbox-only 基线复跑与时间线可视化——那条路径不碰像素。）

## 三、实验数据来源：集成测试脚本推视频

不另做回灌工具。**直接用现有的 [integration_tests/test_single_client.py](../../integration_tests/test_single_client.py) 把清洗视频推进在线链路跑一遍**，一次跑完就同时得到 `features.jsonl`、raw 段与 sidecar——三者天然同源同帧。脚本已有 `--video_path` 与 `--current-step`，不需要为此改脚本：

```bash
python integration_tests/test_single_client.py \
    --scenario 1 --task_id <未占用的 id> --current-step 2 --video_path <清洗视频>
```

两条使用注意：

- **`--task_id` 必须用未占用的 id。** 复用已有 id 会写进同一个 `{task_id}/{step_id}/` 目录，[FeatureStore.open_fresh](../../app/services/inference/feature/store.py) 起 run 即截断该分区，原数据直接没了。
- 脚本收尾的 `cleanup_test_task` **只删 DB 任务行，不动存储目录**，所以跑出来的特征与视频段会留在盘上；lab 的存储侧任务列表照样能列到。

一条必须说清的语义，避免日后误读：**这样产出的是一份新的、自洽的数据，不是历史数据的「修复」。** 推流跑出的 YOLO 结果与该视频当初实时跑的不保证逐帧相同（采样帧集合、模型版本、批次调度都可能不同），墙钟也是本次运行的时刻而非原始作业时刻。实验只要求 features 与 raw 在**同一份数据内部**严格对齐，这一点天然满足。

## 四、融合特征导出器

### 4.1 定位

**本期导出器只用于可行性验证：产出不同方案的模型输入样例，用来回答「哪个 recipe 值得继续」。** 具体训练流程不在本仓库，本仓不建训练数据流水线、不管数据集版本治理。

导出器**直接产出喂入模型的内容**（模型输入本身，不是中间半成品），服务两个下游：

```
                     ┌─→ 训练仓（独立仓库）：拿样例确认输入形态，训模型
raw 帧 + features ──→ 导出器(recipe) ─┤
                     └─→ 将来的 Segmenter.preprocess：同一份 recipe 代码
```

**硬约束：单一真源。** recipe 的特征转换代码只有一份，导出走它、将来推理时 `preprocess` 也走它。这是现有 [clean.py](../../app/services/inference/offline/impl/clean.py) 已经在用的形态（`build_base_features` / `add_business_priors` / `add_centered_window_stats` 都是模块级纯函数，被各 Segmenter 的 `preprocess` 组合调用），本次沿用，只是新增的 recipe 多一路帧输入。

> 「只做可行性验证」不豁免这条。样例正是训练仓据以确定输入形态的东西——样例由一份代码产、推理由另一份代码产，skew 就是从这里进来的。

### 4.2 第一批 recipe

三个，逐级加信息量，每一级都必须能独立对照上一级：

| 编号 | 输入 | 目的 |
|------|------|------|
| **R0** | bbox-only，即现有 113 维 `clean_bbox_v2_top1_impute` | **对照基线**。不新写特征，但必须走同一套导出管道产出，否则后面的增益没有可比基准 |
| **R1a** | R0 + 全帧 CNN **深层**（stride-32）全局池化向量，backbone = **YOLO 主干** | 回答「视觉信息有没有用」。特征域与本场景匹配，权重仓库已有 |
| **R1b** | 同 R1a，backbone = **ImageNet ResNet18** | 与 R1a 对照。YOLO 主干与 bbox 同源，其增量只是「检测头丢掉的信息」；ResNet18 才是一条**独立**的视觉通道，两者差异正是 R1 要测的核心变量 |
| **R2** | R1 + 手部 RoIAlign token，从**同一次前向的浅层**（stride-8）特征图上取，最多 K 个 | 刷具检不到时的主要证据来源。手漏检时 token 置零并**显式传 mask**，不能用零向量冒充「画面里没有手」 |

手-物 union 交互 token（提案的 R3）本期不做：R1/R2 还没证明有增益之前做它可能是白工。

**backbone 是导出器的配置项，不是硬编码。** R1a/R1b 只差这一个配置；R2 在选定的 backbone 上叠 token。

### 4.2.1 取特征的层：单次全帧前向，深浅两层各取所需

**一次全帧前向，同时留下深层与浅层两张特征图**（FPN 的常规做法）：

```
raw 帧 ──> backbone 前向一次
             ├── 浅层 stride-8  特征图 ──RoIAlign(手框)──> f_hand   （细节足）
             └── 深层 stride-32 特征图 ──全局池化────────> f_global （语义强）
```

不是「全帧 vs 只裁 ROI」的二选一——两者在本项目的实测数据上**成本几乎相同**（5.6~9.8 ms/帧），差异全在信息量。定这条的依据见 §4.2.2 的现场数据。

一次顺序解码 + 一次前向，同一 backbone 下的多个 recipe 不各解码一遍视频。

### 4.2.2 定型依据：现场数据实测

样本 `database/1785995202505/2`，1886 帧 @640×480，真实 CLEAN 作业。

**① 取深层特征的话，手部根本没有细节可取。** 手框中位 87×98 px，在各层特征图上占的格数（同一次 YOLO 主干前向的不同截断点）：

| 截断层 | 特征图 | stride | 累计耗时 | 手框占格 |
|---|---|---|---|---|
| `model[:3]` | `[1, 64, 120, 160]` | 4 | 3.4 ms | 21.8 × 24.5 |
| **`model[:5]`** | `[1, 128, 60, 80]` | **8** | **5.6 ms** | **10.9 × 12.2** |
| `model[:7]` | `[1, 128, 30, 40]` | 16 | 6.6 ms | 5.4 × 6.1 |
| `model[:10]` | `[1, 256, 15, 20]` | 32 | 9.8 ms | 2.7 × 3.1 |

stride-32 上手框只有 2.7×3.1 格，RoIAlign 到 7×7 基本是插值放大。**换到 stride-8，格数翻 4 倍，而且更便宜。** 手部细节不足的根因是取特征的层太深，不是输入范围太大。

**② 纯 ROI crop 的代价在这份数据上是硬的。** 全帧一次 9.8 ms vs 手框 crop 128²×2 过 ResNet18 的 7.0 ms——只省 2.8 ms，算力不构成理由；而代价是：

- **19.4% 的帧一只手都没检到**（366/1886），最长连续 259 帧 ≈ 34.5 s @7.5fps。纯 ROI 下这些帧零视觉特征；
- 彻底丢掉全局上下文，而动作可能发生在清洗池外；
- crop 的招牌优势「尺度归一化」在固定机位下几乎不值钱——实测手框等效边长 p5/p95 = 76/119 px，**尺度跨度仅 1.6×**。

**③ 逐帧 hand 数分布**（决定 R2 的 K）：0 手 19.4% / 1 手 20.4% / 2 手 59.9% / ≥3 手 0.3%。**K=2 覆盖 99.7%**，提案里「K=2 待验证」由此确认。

### 4.2.3 backbone 选型实测（2026-08-14，Apple M 系 arm64 / 10 线程 / 640×640 / 预热后 20 次均值）

| backbone | 参数 | CPU 前向 | 输出特征图 | 权重来源 |
|---|---|---|---|---|
| **YOLO 主干**（`clean-large-best.pt` 前 10 层至 SPPF） | 1.12M | **10.9 ms** | `[1, 256, 20, 20]` | 仓库 [app/data/](../../app/data/)，零下载 |
| **ResNet18**（`channels_last`） | 11.7M | **21.5 ms** | `[1, 512, 20, 20]` | ImageNet，45MB |
| MobileNetV3-Small | 2.54M | 95.9 ms | `[1, 576, 20, 20]` | ImageNet，10MB |
| MobileNetV3-Large | 5.5M | 134.7 ms | — | ImageNet |
| （参照）YOLO 完整含 head | 2.59M | 22.9 ms | — | — |

**结论：MobileNetV3 出局。** 参数与 YOLO 主干相当、FLOPs 少一个数量级，实测却慢 9 倍——depthwise separable conv 在 CPU 上是访存瓶颈、走不到优化过的 GEMM 内核，而 YOLO 的 C2f 与 ResNet 的稠密卷积直接命中 oneDNN/ARM 快路径。`channels_last` 对它不但无效反而更慢（139.9 ms）。**MobileNet 的「轻量」是按 FLOPs 与手机 NPU 定义的，在本项目的 CPU 部署形态下不成立。**

这一条覆盖了[模型提案](20260814_CLEAN_STAGE_MODEL_PROPOSAL.md) §3.2A 把 MobileNetV3-Small 列为计算量基线的写法——该处本就声明「不提前定为最终 backbone，以目标设备离线吞吐实测决定」，现已实测。

三个 backbone 的深层输出都是 stride-32，浅层都能取到 stride-8，§4.2.1 的深浅两层取法与 backbone 选型正交。

按全帧 640×480 一次前向 9.8 ms 估算，一条 10 分钟 step（7.5fps ≈ 4500 帧）CPU 上约 45 秒跑完——**可行性验证阶段 GPU 用不上**。

### 4.3 产出物

每个 `(task_id, step_id, recipe)` 一份产物，含两部分：

- **特征**：`[T, F]` 数值矩阵（R2 另含 token 与其 mask），T 与 `features.jsonl` 的帧序一一对应；
- **manifest**：`feature_version`、`recipe` 名、backbone 标识与输入分辨率、`feature_names`、逐帧 `ts`、`frame_wh`、以及**质量统计**（总帧数、sidecar 命中数、因取不到帧被 mask 的帧数、落在不可取帧区间的帧数）。

质量统计是必需项不是可选项：一条 step 有多少帧其实没拿到像素，必须一眼看到，不能等到模型不收敛时才回头查。

**落盘位置**：**不能**放在 `{storage_base_dir}/{task_id}/{step_id}/`。该目录受 `cleanup_days`（默认 7 天，[persistence/config.py](../../app/services/persistence/config.py)）的 TTL 回收，样例产物放进去会在无人察觉时消失。

落 `{storage_base_dir}/.offline_exports/{task_id}/{step_id}/{recipe}@{backbone}/`，CLI `--out-dir` 可覆盖。**不新增配置项**——导出器是手动跑的实验工具，CLI 参数就是它的旋钮；默认路径由 `storage_base_dir` 派生，与 [ClipBuilder](../../app/services/lab/clip_builder.py) 默认 `{base_dir}/.lab_exports` 同款约定。

**算力**：导出器带 `--device` 开关，**默认 `cpu`**（沿用现有 [offline/cli.py](../../app/services/inference/offline/cli.py) 的隔离取向，不抢在线资源）。按 §4.2.3 实测，可行性阶段 CPU 完全够用，GPU 只是批量重抽特征时的加速项。注意现有 CLI 是在任何 `torch` import 之前硬置 `CUDA_VISIBLE_DEVICES=""`，导出器要允许 GPU 就不能照抄这段，得让隔离可配。

## 五、实验平台（原 lab 页面）

[lab 页面](../../app/static/lab/index.html) 从「送标工作台」**重命名为「实验平台」**，在现有送标能力之外，新增一块离线实验区。

> 明确**不进运维面板**：运维面板面向线上值守，实验的迭代节奏和它完全不同，混在一起两边都难改。

离线实验区要能做到：

1. 列出可实验的 `(task_id, step_id)`——即有 `features.jsonl` 的 step，并显示是否带 sidecar（决定能不能做融合）；
2. 手动触发：跑特征导出 / 跑离线推理，可选 recipe 与策略；
3. 看**清洗时间线**：多泳道并排——各策略的预测分段、逐帧置信度条；
4. 点时间线上任一区间，**直接拉对应 raw clip 回放**核对切分对不对。复用现有的 [ClipBuilder](../../app/services/lab/clip_builder.py) 与页面已引的 `hls.js`。

「点段回放」是这块的核心价值，不是锦上添花——没有回放就只能盯着色块猜切分对不对，核对无从谈起。

触发离线推理是分钟级长任务，接口须异步返回 + 状态轮询，不能让页面同步等。

## 六、硬不变式

| | 不变式 |
|---|---|
| **F1** | `ts` 是 feature 与像素帧之间**唯一**的 join key。不引入第二套帧标识，不做「按最近时间 seek」 |
| **F2** | sidecar 的 `frame_ts[i]` 必须与该段解码出的第 `i` 帧严格对应。写入顺序即解码顺序，中间不得有任何重排 |
| **F3** | 导出器的特征转换代码与将来 Segmenter `preprocess` 用的**是同一份**。任何「训练仓自己抄一份特征工程」都是 train/serve skew 的直接来源 |
| **F4** | 取不到帧一律显式 mask 并计入统计，**不得**用零向量、邻帧或插值冒充真实帧 |
| **F5** | 产物与 recipe 版本、backbone 标识绑定写进 manifest。换 backbone 或改 recipe 必须换版本号，不得原地覆盖 |
| **F6** | 导出物不落在受 TTL 回收的 step 目录下 |
| **F7** | 本期不改 `features.jsonl` 契约、不改在线链路、不改 6 类动作标签集 |

## 七、待确认

**无阻塞项。** 原「backbone 预训练权重从哪来」已消解：R1a 用仓库已有的 YOLO 权重、零下载；R1b 的 ResNet18 ImageNet 权重实测可直接从 `download.pytorch.org` 拉到（10.3MB 的 MobileNet 用时 0.6s，同源 CDN），开发/实验机无障碍。

唯一遗留的是部署侧尾巴，且不阻塞开工：**若要在离线 Linux GPU 主机上跑 R1b**，45MB 的 ResNet18 权重得随物料分发（[install.sh](../../DEPLOYMENT.md) 物料清单）。R1a 无此问题。可行性阶段若只在能联网的实验机上跑导出，这条不需要处理。

## 后续计划

1. S1 sidecar 落地 → 用集成测试脚本推一段清洗视频，先确认 sidecar 命中率与丢帧统计健康；
2. 导出器 R0 打通全链路（此时不需要像素），产出样例确认输入形态；
3. R1a/R1b/R2 接入；
4. 实验平台离线实验区；
5. 实验有结论后，再写融合 Segmenter——此时 recipe 代码已就位，Segmenter 只需加载 checkpoint 与解码分段；
6. 下一期：Label Studio 区间标注读回、segmental F1/Edit/边界误差对照。

## 验证

本次只新增需求文档，未修改代码、配置或模型，未运行测试。
