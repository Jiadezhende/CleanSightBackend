# 离线动作分割接入 ROI 视觉特征：核心数据契约 + 特征提取流程

> **变更状态**：提案（2026-09-05）　<!-- 本期只落数据契约与提取流程，不接融合训练 -->
> **知识库**：无需沉淀（提案；落地后另行沉淀）
>
> 相关：[20260906_OFFLINE_DATA_MODEL_NAMING.md](20260906_OFFLINE_DATA_MODEL_NAMING.md)（**数据模型正名，本篇的前置**：`ModelInput` → `FeatureSequence`、`FrameTracker` → `FrameLocator` 等；本文暂用现名，正名落地后统一跟进）、[20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md](20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md)（离线链路一期）、[20260830_FRAME_TRACKER_SIDECAR.md](20260830_FRAME_TRACKER_SIDECAR.md)（像素反查）、[20260905_TEMPORAL_MEMORY_BOUNDS_PROPOSAL.md](20260905_TEMPORAL_MEMORY_BOUNDS_PROPOSAL.md)（**内存安全边界**：其 D1/D5/D6 已于 2026-09-06 落地，本文的现状描述以落地后为准；未落地的 ASFormer O(T²) 归那篇，本文不重复）。

## 概述

离线动作分割当前只吃 bbox 几何特征。本提案给它加一路 ROI 视觉特征：按槽位从原始帧裁剪目标区域、过视觉 encoder 得逐帧 embedding，落成独立二进制产物，供后续与 bbox 特征在模型入口融合。

**本期范围**：公共面只有两件——`SlotTracks` 契约（新建 `offline/types.py`）+ ROI 反查提取工具（新建 `offline/visual.py`，含 npz 落盘格式），外加提取流程的 CLI 入口。embedding 怎么压、怎么和 bbox 特征拼，**归各模型自己实现**，不进公共层。**不含**融合模型、训练、自动触发。

## 变更背景

- **现状**：[`build_base_features`](../../app/services/inference/offline/impl/clean.py) 把 `List[FrameFeature]` 转成 113 维几何特征（位置/面积/速度/目标对距离），三种时序模型（MS-TCN+BiLSTM / ASFormer / BiGRU）在此之上各叠一层 recipe。全部信息来自检测框的坐标与置信度。
- **痛点**：几何特征区分不了"形似而事不同"的动作。刷头在镜体附近往复运动这件事，几何上和其它器械贴近镜体高度相似；`add_business_priors` 里那 8 维手工先验就是在补这个洞，但补不动——框的位置不携带"框里正在发生什么"。
- **可行性**：像素通路已经通了。[`FrameTracker.find(timestamps)`](../../app/services/inference/offline/frame_tracker.py) 能按 ts 反查原始帧，且要求 ts **位级等于** `features.jsonl`，两侧同源。离线 CLI 已是独立 CPU-only 进程，不抢在线 GPU。
- **承接**：建立在 `FrameFeature` 帧级货币（[20260717_FRAME_FEATURE_WINDOW.md](20260717_FRAME_FEATURE_WINDOW.md)）与 HLS 段级 sidecar 索引之上。

## 方案详情

### 特征提取流程（主干）

一条离线 CLI 命令，三步，全部在 CPU-only 子进程内完成：

```text
extract --task-id T --step-id S
  ① 建裁剪计划（全量、纯 CPU、无 I/O）
     frames = FeatureStore.load(T, S)      # [T] FrameFeature：稀疏变长的检测记录
     tracks = assign_slots(frames)         # SlotTracks：定长 T、K 条归一化 xyxy 轨迹
  ② 流式解码 + 编码（单遍，crop 即算即弃）
     embed  = np.full([T,K,D], nan, fp16)  # 预分配
     for frame in FrameTracker(T,S).find(ts_list, W, H):   # ts 升序，不支持随机访问
         row   = frame.timestamp → 行号（按 ts 对号，不按位置）
         crops = 按 tracks.boxes[row] 裁剪 + padding + resize   # 缺席槽跳过
         积满一批 → encoder 前向 → 写回 embed[row, k]
  ③ np.savez(visual_roi_v1.npz, ts=..., embed=...)
```

**① 必须先于 ② 全量算完**：`FrameTracker` 按 ts 升序产出、不支持随机访问，单遍流式是硬约束，裁剪计划没法边解码边定。① 内部两句合为一步是因为它们之间没有值得分辨的边界——都是纯 CPU、都在解码前；但**代码上仍是两个函数**：`FeatureStore.load` 是框架层共享 I/O（Runner 对所有策略一视同仁 load 一次，还要拿裸 `frames` 做内存准入闸），`assign_slots` 是 clean 专属配方，不能倒灌进 load。

下面各节是这三步的实现细节，逐一对应：

| 步骤 | 落在哪 | 详见 |
|------|--------|------|
| 唯一的公共契约 `SlotTracks` | `offline/types.py`（新增） | §1 |
| ① 槽位分配 → 裁剪计划 | `offline/impl/clean.py` 抽出 `assign_slots` | §2（本期唯一重构） |
| ②③ 裁剪 + 编码 + npz 落盘 | `offline/visual.py`（新增） | §3 |
| 命令入口 | `offline/cli.py` 新增 `extract` | §4 |
| 全流程内存 / 耗时 | — | §5 |

### 设计决策

**存储层不融合，融合只发生在模型入口。** bbox 与视觉两侧数据性质、生命周期、演化速度都不同：前者是稀疏可变长的语义记录、由在线链路常开写入；后者是定长稠密数值阵列、离线派生可随时重算丢弃。焊成一个文件意味着任一侧改动都要整体重来。

| 方案 | 代价 | 结论 |
|------|------|------|
| A：视觉独立落盘，内存融合（采用） | 多一份磁盘产物、多一个 CLI 阶段 | 最贵的一段（ffmpeg 解码 + encoder 前向）成为可缓存产物，换时序模型不重跑 |
| B：视觉塞进 `features.jsonl` | `[T,K,D]` 走 JSON 约 30KB/帧、精度损失、解析开销 | 否 |
| C：在线写回口顺手编码 | GPU 成本压进在线链路，ROI 配方一改要重新采集 | 否，与"离线可反复重算"相反 |

**同理，落盘的是 embedding 不是拼好的融合矩阵**——对齐现有"落原始框、不落算好的 113 维"的不变式，配方演进不必重采数据。

### 1. `offline/types.py`（新增）— `SlotTracks`，本期唯一的新公共契约

公共面只有两件：**`SlotTracks` 这一个数据契约**，加**一个 ROI 反查/提取工具**（§3）。其余全部是 embedding 阶段的事，归各模型自己实现。

```python
@dataclass(frozen=True)
class SlotTracks:
    """整段的 K 条槽位轨迹：定长 T、归一化坐标，只装观测不装推导。"""
    slots: Tuple[str, ...]  # K 个槽位名；顺序即 boxes/conf/embed 的 K 维通道序
    ts:    np.ndarray       # [T]      float64，位级同源于 features.jsonl
    boxes: np.ndarray       # [T,K,4]  归一化 xyxy（已 clamp/排序），缺席 = NaN
    conf:  np.ndarray       # [T,K]    float32，缺席 = NaN
```

**只装观测，不装推导。** `present` = `~isnan(boxes[...,0])`，`cx/cy/area` 由消费方从 `boxes` 现算——不设字段。`boxes` 存的已是 clamp + 排序之后的归一化 xyxy，从它重算 `cx=(x1+x2)*0.5`、`area=bw*bh` 与现有 [`_bbox_to_center_area`](../../app/services/inference/offline/impl/clean.py) 是同一串浮点运算作用在同一批输入上，**逐位相同**，不影响本期赖以立身的等价保证。

**为什么它必须公共**：两个跨模块消费者——`build_base_features`（纯 bbox，在 `impl/`）与 ROI 提取（在 `visual.py`，框架层）。留在 `impl/clean.py` 就会逼出"框架层 import impl"，或者把 `visual.py` 入参降级成裸 ndarray 去绕开 import；后者是拿 API 难看换分层，说明类型放错了边。

**`ModelInput` 不迁，留在 `impl/clean.py`。** 它没有当公共契约的资格——两头都不是：

- **不是原始真相**：那是 `SlotTracks`。
- **不是能直接推理的张量**：[`_predict_with_model`](../../app/services/inference/offline/impl/clean.py) 拿到它之后还要过 normalizer、`nan_to_num`、加 batch 维才进 torch；中间还隔着 `add_business_priors` / `add_centered_window_stats` 两层 recipe。

它是一个半成品 recipe 中间态，列名口径、`feature_version`、recipe 叠加顺序全是 clean 私有。把它提到公共层，只会让下一个 stage 误以为该套这个壳。`ModelInput.visual` 字段同理，是 clean 内部的事（§3 末）。

相应地，[segmenter.py](../../app/services/inference/offline/segmenter.py) 的 `preprocess -> Any` / `segment(model_input: Any)` 与文件头「不自定义中间数据壳」的约束**一律不动**——中段本就该是策略私有（[mock.py](../../app/services/inference/offline/impl/mock.py) 的 `preprocess` 直接透传 `Sequence[FrameFeature]`，那是对的）。`SlotTracks` 不是 `preprocess` 的产物，是它的**上游原料**，不受这条约束管辖。

### 2. `offline/impl/clean.py` — 槽位分配提成共用纯函数（步骤 ①）

**这是本期唯一的重构，也是绕不开的前置。** 现在 [`_select_hand_slots`](../../app/services/inference/offline/impl/clean.py) / [`_select_top1_slot`](../../app/services/inference/offline/impl/clean.py) 是 `_build_feature_matrix` 的内部步骤，且 [`_collect_object_buckets`](../../app/services/inference/offline/impl/clean.py) 只保留 `(present, cx, cy, area, conf)`——**原始 xyxy 在这一步就被丢掉了**，裁不了 ROI。

更要紧的是身份一致性：槽位分配带 `prev_center` 位移惩罚（[`_box_score`](../../app/services/inference/offline/impl/clean.py)），是个弱跟踪。若视觉侧另算一遍，bbox 侧的 `hand_top1_*` 和视觉侧的 slot 1 不保证是同一只手——静默错。

改法：抽出 `assign_slots(frames, ...) -> SlotTracks`（契约见 §1），保留归一化后的 xyxy；`build_base_features` 改为消费 `tracks`（`present`/`cx`/`cy`/`area` 从 `tracks.boxes` 现算），**逐值输出不变**（沿用已有回归测试）；ROI 提取消费同一份 `tracks.boxes`。

**留在 `impl/clean.py` 的是实现，不是契约**：槽位名单（`OBJECTS`）、hand top-2、`_box_score` 打分规则全是 clean 配方，换个 stage 就是另一套。契约公共、配方私有。

**产出不是"筛过一遍的 `FrameFeature`"。** 槽位分配做的是坐标系转换（稀疏变长的检测记录 → 定长 `[T,K]` 带洞矩阵），不是对框做筛选加重排——三点决定了它塞不回 `FrameFeature`：

| 差异 | `FrameFeature` | `SlotTracks` |
|------|----------------|--------------|
| 缺席 | 表示不了。没有 `Detection` 就是"没检到"，无法表达"slot 1 这一帧空着" | 缺席是轴上的一个位置（`boxes` = NaN），[`_impute_short_gaps`](../../app/services/inference/offline/impl/clean.py) 的线性补帧、`missing_age` 全靠它 |
| 跨帧身份 | `by_source[...].detections` 是无序 list，要表达"slot k 跨帧是同一只手"只能靠下标——没人遵守的隐式契约 | 身份就是第二维 K，由 `prev_center` 弱跟踪产生，是这一步**新造出来的信息** |
| 坐标 | `Detection.bbox` 是 `List[int]` 像素 | 归一化 float xyxy——ROI 裁剪必须用它，因为解码分辨率 ≠ 原始帧分辨率 |

> **复用已有的等价安全网**：D1 落地时已建 [`tests/test_offline_feature_scale.py`](../../tests/test_offline_feature_scale.py)，里面钉着「整条 `build_base_features` 的 113 维矩阵逐值全等」。本次抽 `assign_slots` 改的是同一处接缝，直接沿用这份测试，不另起一套。

clean 的裁剪配方只剩一个模块级常量 `CLEAN_ROI_PADDING`（框外扩比例）落在同文件。**裁哪些槽位不设开关**：`assign_slots` 产出的 K 个槽位全裁，通道序即 `SlotTracks.slots`——这样 K 只有一个真源。将来真要裁子集，在调用点切片即可，不必为此再立一份名单。

### 3. `offline/visual.py`（新增）— 裁剪、编码、落盘（步骤 ②③）

只干三件事：按 `SlotTracks.boxes` 裁剪 + resize、encoder 前向、npz 读写。它 import `offline/types.py`，方向是框架层 → 框架层，不碰 `impl/`。

**归一化坐标是正确货币**：解码分辨率（`Timeline.iter` 的 `width/height`）不必等于原始帧分辨率，归一化 xyxy 在两者间自由换算。

**crop 即算即弃是硬约束**：批缓冲只许持有 B 个 crop，绝不累积。攒完再统一编码 = `T×K` 个 crop = 12.6 GB（见 §5）。附带：`frame.frame[y1:y2, x1:x2]` 是**视图**，持有它等于持有整帧 0.88 MB —— 必须 resize 成新数组后立刻释放 `Frame`。

**落盘格式**：
```
{storage}/{task_id}/{step_id}/visual_roi_v1.npz
    ts     float64 [T]        帧时间戳，位级同源于 features.jsonl
    embed  float16 [T, K, D]  缺席槽 = NaN
```

- 文件名即特征方案名，多方案靠文件名共存（`visual_roi_v1` / `visual_roi_v2`）。
- **缺席用 NaN，不用零向量**：零向量是合法 embedding，用它冒充"无此物"会静默混淆缺席与退化；NaN 的失败模式是整个矩阵炸开，而不是看起来正常。encoder 出口做 L2 归一化，fp16 不溢出且自然不产生 NaN。
- `ts` 保留：它不是元数据是身份，唯一把这个 npz 绑到那一次 run。删了之后陈旧 npz 配上被 `open_fresh` 重写过的 `features.jsonl` 会静默错位。
- **不做加载期校验**：npz 里不记裁剪参数，也不比对。改了 padding / 换了 encoder 就手动删 npz 重跑。单人手动跑，纪律成本低于机制成本。

`ModelInput.visual` 字段（本期只定义，不消费）：`[T, D_v] float32`，已对齐到 `timestamps`。

`[T,K,D] → [T,D_v]` 的压缩（flatten / pool）留给各模型在自己 `preprocess` 里做，与现有 `add_business_priors` 同一层级；对齐用 `np.searchsorted(ts, timestamps)` 现算。版本走现有 `feature_version` 拼接：`"clean_bbox_v2_top1_impute+roi_v1"`，checkpoint 校验路径一行不动。

**独立字段，不并进 `features`。** 两条，都是硬的：

- **拷贝次数**：recipe 链路 `base → center_window → priors` 每步 `np.concatenate` 重建整个矩阵，峰值并存 3 份（`_FEATURE_MATRIX_COPIES`）。`K×D=2560` 列视觉拼进去，就是这 3 份拷贝各背一份视觉数据。
- **列名语义**：`features` 每列都有名字，`feature_names` 是 checkpoint 校验的比对项。2560 个 embedding 分量没有名字可给，硬塞会把校验路径撑成噪声。

### 4. `offline/cli.py` — 新增 `extract` 子命令

与现有 `run` / `query` 并列，复用 `_isolate_cpu()`（CPU-only + 限核，须在任何 torch import 之前）：

```bash
python -m app.services.inference.offline.cli extract --task-id 100 --step-id 2
```

### 5. 资源预算（10 min step，T=9000，K=10）

T 的口径：`features.jsonl` 按 `settings.inference_fps = raw_fps/inference_decimation = 30/2 = **15fps**` 写入，离线 `preprocess` **不重采样**（`build_base_features` 吃全量 frames，`self.fps` 只作 speed 的兜底分母）。故 10 min → T=9000。

| 项 | 量级 | 性质 |
| ---- | ------ | ------ |
| ffmpeg 解码 | 同时只活 1 帧，640×480 bgr24 = 0.88 MB | 流式，与时长无关 |
| ffmpeg stderr 临时文件 | KB 级（`-loglevel error`），按段开关 | 全流程唯一临时文件 |
| `embed` 预分配 fp16 [T,K,D] | D=256 → 44 MB；D=576 → 99 MB | **O(T) 主导项** |
| `SlotTracks`（`boxes` + `conf` + `ts`） | 1.5 MB | 可忽略；去掉推导列后比原方案再省一半 |
| `FeatureStore.load` 全量常驻 | 数十 MB | O(T) |
| `np.savez` | numpy 1.26 直接流进 zip（16 MB 分块） | 无临时文件、无整份副本 |
| **反例：攒全量 crop 再编码** | **12.6 GB** | 禁止 |

提取峰值 ≈ 400 MB（含 torch 基线），npz 落盘 44 MB/step。均线性于时长，1 小时 step 仍在百 MB 量级。

解码放大（时间成本非内存）：`Timeline.iter` 解码区间内每一帧，raw 轨 30fps 而只需 15fps 的一半 → 10 min 解 18000 帧丢一半。

### 保留项（不改动）

- `features.jsonl` 格式、`FeatureStore` / `FactLedger` / `SegmentFact` 全部不动。
- 三个现有 segmenter 的 `preprocess` / `segment` 不动；本期不接融合。
- `OfflineSegmenter` 基类**签名不动**：`preprocess -> Any` / `segment(model_input: Any)` 保持不定型，只改一行过时的文件头注释（见 §1）。
- `mock.py` 不动：它的 `preprocess` 直接透传 `Sequence[FrameFeature]`，规则型策略没有特征矩阵，本就不该套壳。
- `ModelInput` **不迁位置、不改名**：仍是 `impl/clean.py` 私有，字段语义 / `FEATURE_VERSION` / checkpoint 校验逻辑一律不动，只加一个 `visual` 字段。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| 离线特征来源 | 仅检测框坐标/置信度 | + 逐帧逐槽位 ROI 视觉 embedding |
| 槽位分配 | `build_base_features` 内部临时量 | 提成共用纯函数，两侧共享同一身份 |
| 离线公共面 | 只有基类两端（`FrameFeature` 进、`SegmentFact` 出），中间全是各策略私货 | 多一个 `SlotTracks` 契约 + 一个 ROI 提取工具；embedding 与特征拼装仍归各模型 |
| 重跑代价 | — | 解码+编码结果落盘可复用，换时序模型不重跑 |

**自测计划**

| 项 | 方法 |
|----|------|
| 重构安全网 | `assign_slots` 改造后 `build_base_features` 输出与改造前**逐值相等** |
| 契约往返 | npz 写入→读回，`ts` 位级相等、缺席槽为 NaN |
| 裁剪边界 | 框贴边/越界/零面积时 padding 后的 clamp 行为 |
| 全量回归 | `pytest tests/`（先激活 `.venv`） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **ASFormer 前向 O(T²)（既有问题，与本提案无关）** | `MultiheadAttention` 无 `attn_mask`，长 step 走 ASFormer 会被 Runner 预算闸挡下（期望行为，非崩溃） | [内存边界提案](20260905_TEMPORAL_MEMORY_BOUNDS_PROPOSAL.md) 的 D2/D3，尚未落地；同提案 D1/D5/D6（特征构建 O(T²)、准入闸、`features` 改 ndarray）已于 2026-09-06 落地 |
| **encoder 选型未定** | 直接决定提取耗时 | 见下方单列 |
| **色彩序** | 静默错：`Timeline` 解出的是 **bgr24**，torchvision 系预训练权重吃 RGB，转错不报错只是特征全错 | encoder 入口显式断言 + 转换 |
| `frame_width/height` 为 `None` | 归一化兜底错只是数值偏移，**裁剪兜底错就是裁到别的地方** | ROI 路径遇 `None` 硬失败，不复用 640×480 兜底 |
| HLS 段被 TTL 清理 | `FrameTracker.find` 抛 `ValueError`，整段提取失败 | 本期接受硬失败；提示改用未过期的 step |
| 融合、训练、`ModelInput.visual` 消费 | 本期不做 | 下一期 |
| stride 降采样、全局帧 embedding 槽、方形化策略、npz 记裁剪参数并在加载期校验 | 本期刻意不做 | 有需要再加 |

### 待定：encoder 选型与 CPU 预算

离线 CLI 是 CPU-only、默认 2 线程（[`_isolate_cpu`](../../app/services/inference/offline/cli.py)），这是设计约束不是可调项——不能抢在线 GPU。提取量级：10 分钟 step @ 15fps = 9000 帧 × K 个槽位，缺席后实际裁剪数约为上限的 1/3。

| 选型 | 单 crop 量级 | 全段量级 | 备注 |
|------|-------------|---------|------|
| MobileNetV3-small @112×112 | ~3ms | 数分钟 | 推荐起步 |
| ResNet18 @224×224 | ~25ms | 数十分钟 | 语义更强，单次跑可接受、迭代嫌慢 |

倾向 MobileNetV3-small 起步：本期目标是把通路和契约跑通，encoder 换掉只是删 npz 重跑，代价已被"独立落盘"的设计吸收。`torchvision==0.23.0` 已在 [requirements-cpu.txt](../../requirements-cpu.txt)，无新依赖。

本期只落一个具体 encoder，**不预先抽 `RoiEncoder` 基类**——出现第二个 encoder 时再抽，那时才知道接缝该切在哪。
