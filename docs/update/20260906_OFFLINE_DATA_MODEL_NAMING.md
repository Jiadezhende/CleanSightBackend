# 离线链路数据模型正名：把「feature」一词收归给算出来的特征

> **变更状态**：提案（2026-09-06）　<!-- 三期工程；A 档可立即做，B 档需拍板是否同期 -->
> **知识库**：无需沉淀（提案；落地后另行沉淀）
>
> 相关：[20260903_PACKAGE_LAYOUT_SPEC.md](20260903_PACKAGE_LAYOUT_SPEC.md)（**同类改名的既有原则与 playbook**，本篇直接沿用）、[20260905_OFFLINE_ROI_VISUAL_PROPOSAL.md](20260905_OFFLINE_ROI_VISUAL_PROPOSAL.md)（ROI 视觉特征，本篇是它的前置）、[20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md](20260628_OFFLINE_PIPELINE_PHASE1_PROPOSAL.md)（离线链路一期）。

## 概述

离线链路里「feature」同时指三样东西：磁盘上的原始检测记录、算出来的 113 维几何量、即将加入的视觉 embedding。本提案把**观测侧**统一改叫 detection/record，把 `feature` 一词**专留给算出来的特征**，并顺带修掉 `FrameTracker` / `Timeline` 两处名实错位。按影响面分三档，A 档立即可做。

## 变更背景

- **现状**：`FrameFeature` 装的是 `ts + {流名: FrameDetections}`——**它的 docstring 自己写着「持有的是对齐后的检测（非计算特征）」**（[detection.py:42](../../app/domain/detection.py#L42)）。`FeatureStore` 把它落进 `features.jsonl`，注释写「bbox 即特征」。而真正算出来的特征在 `ModelInput.features` / `feature_names` / `feature_version` 里。同一个词，三层不同含义。
- **痛点**：这不是洁癖问题。三期 ROI 要同时碰这三层——读 `FeatureStore` 拿"特征"、算 `build_base_features` 出"特征"、再落一份视觉"特征" npz。评审时「这个 feature 指哪个」要靠上下文猜，是实打实的正确性风险面。
- **二次撞词**：三期新增 `SlotTracks`（K 条槽位轨迹，由 `_box_score` 的 `prev_center` 弱跟踪产生）。而 [`FrameTracker`](../../app/services/inference/offline/frame_tracker.py) 干的是「按 ts 反查原始帧」，跟 tracking 毫无关系。两个 track 撞在同一条链路上，`FrameTracker` 会被读成"产生 tracks 的东西"。
- **触发来源**：三期工程动手前的数据模型梳理。
- **承接**：[包结构规范](20260903_PACKAGE_LAYOUT_SPEC.md)期 3 已经做过同一类事（`inference/models.py` → `types.py`），并写下了判据与手法，本篇沿用、不另立规矩：

  > 「`models` 这个名字在本代码库里已被『DB 行映射』占用。再让 `services/*/models.py` 表示『进程内 dataclass』属同名不同义，读者看到 `models` 无法判断是哪一种。」

  把 `models` 换成 `feature`，这段话原样成立。

## 方案详情

### 全景：一个词覆盖三层

```text
在线链路                              离线链路
  Detection                            
    └─ FrameDetections  一个流一帧的框
         └─ FrameFeature ──写──▶ features.jsonl ──读──▶ FrameFeature
            ① 叫 feature，                ② 叫 features，      ① 同左
               装的是对齐后的检测             装的是①的精简投影
                                                   │
                                                   ▼ assign_slots
                                              SlotTracks         ← 三期新增，名实相符
                                                   │
                                                   ▼ build_base_features
                                              ModelInput.features   ③ 第三个 feature，
                                                 .feature_names        这个才是"算出来的特征"
                                                 .feature_version
                                                   │
                                                   ▼ normalizer + nan_to_num + batch 维
                                              torch tensor      ← 真正的"模型输入"
```

**①②是观测，③才是特征；而 `ModelInput` 又不是模型输入（④才是）。** 本提案的两条改法直接对着这两句：

- 观测侧（①②）去掉 `feature` 一词 → detection / record。
- `ModelInput` 改成描述事物的名字，把"输入"这个角色词让给真正的张量。

下面各节按**影响面分档**，不按模块分：

| 档 | 判据 | 本期是否做 | 详见 |
|----|------|-----------|------|
| A | 仅 offline 内部，`app/` 命中 ≤2 文件 | **做** | §1 |
| B | 跨在线/离线的共享货币，`app/` 命中 17–18 文件 | 需拍板，建议同期 | §2 |
| C | 名实相符 | 不动 | §3 |

### 设计决策

**唯一原则：`feature` 专指"由观测算出来的数值"，观测本身一律不叫 feature。** 这条一旦立住，三层的归属自动确定，不需要逐个争论。它也是[包结构规范](20260903_PACKAGE_LAYOUT_SPEC.md)已确立的判据（一个词在本仓被占用后不得表示第二种东西）在 `feature` 上的应用，不是新规矩。

**落盘文件名一律不动**：`features.jsonl` 保持原名。同规范期 3 已有先例并写明理由——

> 「`lab_runtime_config.json` 不改名：那是落盘文件名，改了会让已部署环境的持久化配置成孤儿。」

已部署机器上存着历史 `features.jsonl`，改名等于旧 step 全部读不回。**类名与磁盘文件名解耦即可**，这不是妥协，落盘格式本就该按兼容性演进而不是按可读性演进。

**为什么建议 B 档同期做，而不是等 ROI 落地后**：三期 ROI 会新增一批对 `FrameFeature` / `FeatureStore` 的引用点（`assign_slots`、`visual.py`、`extract` CLI、新测试）。先落 ROI 再改名 = 这批新代码写一遍旧名、再被脚本改一遍。顺序反过来零额外成本。

### 1. A 档（本期做）：offline 内部，改动面 ≤2 文件

| 现名 | 实际职责 | 错在哪 | 建议 | `app/` 命中 |
|------|---------|--------|------|------------|
| `ModelInput` | `[T,F]` 特征矩阵 + 列名 + ts + fps + version | ①`Model` 无指代（仓里有检测模型/时序算子/三个离线分割模型）；②它**不是**能推理的输入——[`_predict_with_model`](../../app/services/inference/offline/impl/clean.py) 拿到后还要过 normalizer、`nan_to_num`、加 batch 维，中间还隔着两层 recipe | `FeatureSequence` | 1 |
| `FrameTracker` | 按 ts 反查原始帧（`find(timestamps) -> Iterator[Frame]`） | `Tracker` 在 CV 语境专指目标跟踪；它做的是定位查找。三期引入 `SlotTracks` 后二次撞词 | ~~`FrameLocator`~~ → **已落地为 `FrameFinder`**（与 `Step.segments_around` 成对仗：段级找段、帧级找帧），见 [step_store 抽取](20260906_STEP_STORE_EXTRACTION.md) §9 | 1 |
| `Timeline` | 按 ts 区间调 ffmpeg 解 HLS 段、产出帧 | 名字像数据结构（"时间轴"），实为解码器 | **已落地为 `SegmentDecoder`**，且连带搬入 `app/services/step_store/`（同上） | 2 |

`FeatureSequence` 保留 `feature` 一词是**故意的**：它装的正是算出来的特征，符合上面立的原则；`feature_names` / `feature_version` / `FEATURE_VERSION` 一并保留不动。

### 2. B 档（建议同期）：跨链路共享货币

| 现名 | 实际职责 | 错在哪 | 建议 | `app/` / `tests/` / `docs/` 命中 |
|------|---------|--------|------|------------------------------|
| `FrameFeature` | 一帧多流对齐的**检测记录** | docstring 自认"非计算特征"；它是在线滑窗与离线回放共用的帧级货币，是词义重载的源头 | `AlignedFrame`（次选 `FrameRecord`） | 17 / 7 / 12 |
| `FeatureStore` | 把 `FrameFeature` 追加进 `features.jsonl`、回读还原 | 存的是观测不是特征。改后与 `FactLedger` 形成**观测 / 事实**的清晰对仗 | `DetectionStore` | 18 / 6 / 34 |
| `feature/` 包名 | 基础设施包，装 `FeatureStore` + `FactLedger` | 同上；且[包结构规范 §1](20260903_PACKAGE_LAYOUT_SPEC.md) 白纸黑字把它登记为「基础设施包 `feature/`」，改名要连带修订那份规范 | `record/`（→ `record.store`）；**优先级最低，可单独留后** | — |

私有函数 `_feature_to_record` / `_record_to_feature` 随之调整（改后 `_frame_to_record` / `_record_to_frame`，逆运算对仗仍在）。

### 3. C 档（不动）：名实相符

`SegmentFact` / `EventFact` / `FactLedger`（事实 + 账本，准确）、`OfflineRunSpec` / `OfflineRunResult` / `OfflineRunner`、`Detection` / `FrameDetections`、`SlotTracks`（三期新命名，已按本原则取）。

**列出它们是为了划定边界**：本提案不是"把所有名字重排一遍"，只动上面两档共六个符号。

### 4. 落地手法（沿用期 3 playbook，不自创）

1. `git mv` 改文件名（仅 B 档的包重命名涉及），**保留 git 历史**。
2. 脚本批量重写 `app/` `tests/` `integration_tests/` `scripts/` 的符号引用——期 3 同一手法覆盖了 27 个 .py 文件，已验证可行。
3. 全词匹配，避免误伤：`FeatureStore` → `DetectionStore` 安全；但 `feature` 单词本身**不得全局替换**（`feature_names` / `feature_version` / `FEATURE_VERSION` / `build_base_features` 必须保留）。这是本次最大的操作风险，见「遗留风险」。
4. docstring 与 `__init__.py` 分类段跟进；[包结构规范](20260903_PACKAGE_LAYOUT_SPEC.md) §1 的子包分类表若动包名则同步修订。
5. `docs/` 按既有约定处理：`docs/update/` 本篇留档，`docs/kb/` **不在本轮动**（KB 只在维护流程里更新），此条即是给下次 KB 融合的输入。

### 保留项（刻意不改）

- `features.jsonl` / `facts.jsonl` 磁盘文件名与 record schema，一个字节不动。
- `feature_names` / `feature_version` / `FEATURE_VERSION` / `build_base_features` / `inference_fps` —— 这些指的确实是"算出来的特征"，按本提案的原则**应当保留**。
- `OfflineSegmenter` 基类签名（`preprocess -> Any` / `segment(model_input: Any)`），中段仍是策略私有。
- 所有行为、落盘格式、checkpoint 校验逻辑：本提案**纯改名，零行为变更**。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `feature` 一词 | 指观测记录、指磁盘投影、指算出来的特征，三义并存 | 只指算出来的特征；观测侧叫 detection / record |
| 读 `ModelInput` | 以为是能喂进模型的东西 | `FeatureSequence`，一眼看出还差张量化 |
| 读 `FrameTracker` | 与 `SlotTracks` 撞词，像是做目标跟踪 | `FrameLocator`，职责是按 ts 定位 |
| 观测 / 事实两条落盘线 | `FeatureStore` / `FactLedger`，词性不对仗 | `DetectionStore` / `FactLedger`，观测对事实 |

**自测计划**

| 项 | 方法 |
|----|------|
| 零行为变更 | 全量 `pytest tests/`（先激活 `.venv`）逐项通过，且**不修改任何断言值**——只改符号名。断言要改就说明混进了行为变更 |
| 误伤检查 | 改后 grep `feature_names` / `feature_version` / `build_base_features` 命中数与改前一致 |
| 落盘兼容 | 用改名前写出的 `features.jsonl` 跑一次 `offline.cli run`，结果与改名前逐值相同 |
| 包结构门禁 | 期 3 已建的导入门禁测试通过（若动 `feature/` 包名） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **`feature` 一词不能全局替换** | 盲目 sed 会打坏 `feature_names` / `feature_version` / `build_base_features`，且**测试未必立刻红**（列名比对错位可能只在加载 checkpoint 时才炸） | 只按符号全词替换，逐个确认；改后跑「误伤检查」那条 |
| **B 档 `docs/` 命中 34 处（`FeatureStore`）** | 文档面比代码面大，是本次真正的工作量所在 | `docs/update/` 历史记录**不回溯改写**（那是当时的事实）；只改 `docs/api/` 与 README 等现行文档，KB 留给融合流程 |
| B 档做不做、什么时候做 | 延后到 ROI 之后 = 新代码写一遍旧名再改一遍 | **需拍板**。推荐同期做完，理由见「设计决策」 |
| `feature/` 包改名要连带修订已发布的包结构规范 | 规范 §1 子包分类表把 `feature/` 登记为样板 | 优先级最低，可以只改类名、留包名不动；真要改则同 PR 修订规范 |
| 改名期间与 ROI 三期并行开发的冲突 | 同一批文件被两个任务动 | 串行：本篇先落，ROI 在新名字上开工 |
