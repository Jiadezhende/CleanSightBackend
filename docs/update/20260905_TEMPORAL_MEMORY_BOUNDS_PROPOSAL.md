# 时序动作分割的内存安全边界：问题定位与决策

> **变更状态**：部分生效（提案 2026-09-05 → **D1 / D6 / D5 落地 2026-09-06**）
> <!-- 离线三项已有代码兑现 + 回归测试锁死；D2/D3/D4 待重训同批，D7/D8 判定不落。见文末「落地记录」 -->
> **知识库**：待沉淀
>
> 相关：[20260905_OFFLINE_ROI_VISUAL_PROPOSAL.md](20260905_OFFLINE_ROI_VISUAL_PROPOSAL.md)（其「遗留风险」记录的 ASFormer O(T²) 是本文 B 项来源，且其 `assign_slots` 重构与本文 A 项是同一处接缝）、[20260717_FRAME_FEATURE_WINDOW.md](20260717_FRAME_FEATURE_WINDOW.md)（`FrameFeature` 帧级货币与在线帧窗生命周期）。

## 概述

时序动作分割接入前，先定「多长的 step 能跑、跑不动时怎么失败」。实测定位到四个膨胀源，其中离线 `build_base_features` 的检测框收集是 **O(T²) 内存与时间**（10 min step ≈ 8 GB / 12 min，三模型全中，且是一次回归）。本文只给问题定位、决策和证据，不含实现设计。影响面：`offline/impl/clean.py`、`offline/segmenter.py`、`offline/runner.py`、`temporal/operator.py`。

---

## 一、问题定位

### 1.1 显存不在风险面上

「内存显存爆炸」里的显存那一半**当前结构上是零暴露**，但只由两行硬编码守着：

| 路径 | 设备 | 由什么保证 |
|------|------|-----------|
| 在线检测（YOLO） | GPU | [`stage_worker.run_stages`](../../app/services/inference/detection/stage_worker.py) 独立子进程，torch import 前钉 `CUDA_VISIBLE_DEVICES` |
| 在线时序算子 | **CPU 硬编码** | [`TemporalOperator.__init__`](../../app/services/inference/temporal/operator.py) `self._device = torch.device("cpu")` |
| 离线分割 CLI | **CPU-only** | [`_isolate_cpu`](../../app/services/inference/offline/cli.py) 在任何 torch import 前置 `CUDA_VISIBLE_DEVICES=""` |

**所以本文的安全边界是 RAM 边界。** 两个尾巴：① 这个不变式无测试守——改错 `_isolate_cpu` 的调用顺序不报错，只会静默开始抢在线 GPU；② ROI 视觉提案要引入 encoder 前向，是第一个有动机上 GPU 的时序侧组件。

### 1.2 四个膨胀源

| 源 | 位置 | 增长阶 | T=9000（10 min） | 严重度 |
|----|------|-------|------------------|--------|
| **A. 检测框收集稠密化** | [`_collect_object_arrays`](../../app/services/inference/offline/impl/clean.py) | **O(T²) 内存 + O(T²) 时间** | **≈8.1 GB / ≥12 min** | **P0，三模型全中** |
| **B. ASFormer 全局注意力** | [`_make_asformer`](../../app/services/inference/offline/impl/clean.py) | O(T²) 内存 | 5.4 GB | P1，仅 ASFormer |
| C. `ModelInput` 装箱 | `features: List[List[float]]` | O(T·F)，系数 8.1× | 73 MB × 约 3 份并存 | P2 |
| D. 在线帧窗无加载期上限 | [`Operator.window_seconds`](../../app/services/inference/temporal/operator.py) | O(window_seconds) | 今天 10 s → 无风险 | P2，配置面 |

### 1.3 A 的机制：平方藏在一行看似 O(1) 的语句里

```python
for idx, ff in enumerate(frames):          # T 次
    for fd in ff.by_source.values():
        for det in fd.detections:          # 合计 D 次，D = T × 每帧框数
            arr = np.zeros((frame_count, 5), dtype=np.float32)   # ← 代价是 T，不是 O(1)
            arr[idx] = (1.0, cx, cy, area, conf)
            out[obj].append(arr)
```

**内存的平方来自分配**：循环体执行 D 次，读起来是「每个检测框干一件固定的事」，但那件事的开销是 `frame_count`——**外层循环的上界跑进了内层语句**。一个框只有 1 帧有值却占 T 行，填充率 1/T。内存 = `20 B × D × T`。

**时间的平方是另一处独立机制**：[`_select_hand_slots`](../../app/services/inference/offline/impl/clean.py) / [`_select_top1_slot`](../../app/services/inference/offline/impl/clean.py) 的 `for t in range(frames)` 里对该类目标**整段的全部 D_obj 个数组**逐个 `_as_box5(arr[t])`（每候选调两次）。`arr[t]` 本身是 O(1)（连续、stride 寻址），**平方来自循环次数 T × D_obj，不是单次访问代价**——每帧重扫整段所有框，只为捞出属于这一帧的两三个。

两者主导项不同：T=4000 时收集阶段 1.09 s（分配/清零受限）、选槽位阶段 141 s（循环次数受限）。**必须一起修**——只改分配则时间照旧，只改循环则内存照旧。

### 1.4 T 今天没有任何上限

| 环节 | 有无上限 |
|------|---------|
| [`FeatureStore.append`](../../app/services/inference/feature/store.py) / `_JsonlBuffer` | 纯追加，无行数/字节上限 |
| `features.jsonl` 落盘 | 无轮转、无大小上限（`open_fresh` 是每 run 截断，不是封顶） |
| [`FeatureStore.load`](../../app/services/inference/feature/store.py) | 整文件读进 list，无 limit 参数 |
| [`OfflineRunner.run`](../../app/services/inference/offline/runner.py) | `load` 完直接进 `preprocess`，**无任何 T 检查** |
| step 时长 | 由外部切 `current_step` 决定，无上限 |
| 存储 TTL `cleanup_days: 15` | 时间保留策略，删的是**已完成**任务目录，不约束在跑 step 的规模 |

唯一天花板是意外副产物：[`health_monitor/config.py`](../../app/services/health_monitor/config.py) 的 `task_max_duration: 7200.0`（2 小时看门狗拆跑飞任务）。它给出最坏情况 **T ≤ 108,000 帧**，平方项 ≈ **1.17 TB** / 29 小时——比可运行范围高五个数量级，等于没有。且离线 CLI 是手动入口，可指向任意 step 目录，连这个天花板都不经过。

**修完 A 仍需要闸**：装桶把增长拉回线性，但线性 ≠ 有界。2 小时 step 线性下仍约 1.3 GB。

### 1.5 B / C / D 补充

- **B**：`Block.forward` 用 `nn.MultiheadAttention(x,x,x)` 无 `attn_mask`，注意力矩阵在 CPU 实打实物化。这里还藏着一个**与内存无关、今天已在发生**的正确性问题：训练大概率按定长切片喂，后端一次喂 T=9000 的全局注意力已偏离训练分布——即使内存够，长 step 输出也未必可信。
- **C**：代价不只是 8.1× 倍数，还有往返次数。BiGRU 链路 base → center_window → priors → 前向共 4 次全量 `asarray`/`tolist` 转换、峰值约 3 份装箱矩阵并存。
- **D**：在线**构造上有界**（`_slide_window_seconds = max(10s, 各算子最大感受野)`，今天 150 帧）。风险在配置面：`window_seconds` 来自 YAML 无加载期校验，而 [`CleanOperator._advance`](../../app/services/inference/temporal/impl/clean.py) 每 tick 重跑整窗。调到 600 不会崩，会静默拖垮 tick 相位。

### 1.6 病史：A 是一次回归，且有一份废弃的参考实现

| 时间 | 提交 | `_collect_object_*` 形态 | 复杂度 |
|------|------|------------------------|--------|
| 最早 | `FeatureVectorizer._collect_boxes(self, frame)` | **逐帧**收集 | 线性 ✓ |
| — | `d4a3f87 feat: align clean offline feature recipes with tests` | 引入 `_collect_object_arrays`，每框一条全长 `[T,5]` | **O(T²)** ✗ |
| 2026-08-16 | `a23814b`（`feat/feature-exp`，**已废弃**） | 改回按帧装桶 | 线性 ✓ |

`feat/feature-exp` 自 2026-08-10（`1b893ad`）分叉未合回，tip 停在 08-17（HEAD 领先 14 / 它领先 9），且把 `offline/` 重排成 `blocks/ + models.py`，与本分支 package-layout 重构撞在同一目录。

| 提案项 | HEAD | `feat/feature-exp` | 处置 |
|--------|------|-------------------|------|
| A 稠密化 O(T²) | **存在** | 已修（真实 step 397×，逐值全等） | 重做，复用其形态与测试口径 |
| C `ModelInput` 装箱 | **存在** | 已修（`FeatureBlock.values: np.ndarray`） | 重做 |
| `t_norm` skew | **存在** | 已解（v4 删三列，71→68 维） | 采纳删列 |
| B / 预算闸 / 在线上限 / 显存断言 | 无 | **也无** | 纯新增 |

---

## 二、决策

| # | 决策 | 理由 | 约束 |
|---|------|------|------|
| **D1** | A 用**按帧装桶** `Dict[str, List[List[ndarray]]]`，不用稠密 `[T,Dmax,5]`、不用稀疏对 `(frame_idx, rows)` | 三者都是 O(T)、内存都在个位数 MB，**3300× 收益不来自表示的巧妙，只来自不再物化时间轴**。装桶比另两个各少一个概念：不需要 Dmax（也就没有「Dmax 不许写死、否则密集帧静默丢框」这条红线），不需要 `searchsorted` 帧边界 | **逐值等价**是硬前提：候选帧内顺序不变 + `list.sort` 稳定 + `present` 恒为 1 使旧过滤恒真。落地必须带回归测试 |
| **D2** | 分块只发生在**模型前向层**，且只有 ASFormer 分块 | MS-TCN+BiLSTM（138 MB @T=18000）与 BiGRU（33 MB）无内存动机，且感受野无限、分块必然改变输出。修完 A 后特征侧已线性且便宜，无需下沉 | 拼接后逐帧 argmax/softmax 口径不变，`_labels_to_segments` 不动 |
| **D3** | `chunk_frames` **由 checkpoint 携带**（`train_slice_frames`），不进 YAML | 取错不报错、只静默出错标签，故来源必须是「不可能配错」而非「配错会被发现」。既然要重训，训练侧直接写进产物即可 | 缺字段 → 加载期 `ValueError`，**不给默认值、不退回全局注意力**。现存 ASFormer 权重会加载失败——这是期望行为 |
| **D4** | **删掉 `t_norm/t_sin/t_cos`**，113→110 维 | 三列一个自由度占三维、一个 ts 都没读；`t_norm` 要总帧数（因果链路算不出）、把 step 时长归一（2 min 与 10 min 段里同一个 0.5 差 4 倍绝对时间）。且训练切片内 0→1 vs 后端整段 0→1，**今天已 skew**。删完后特征管线最大依赖窗口约 7 帧，全局项归零 | 版本名 `clean_bbox_v2_notime`，**刻意不叫 v3/v4**——废弃分支的 v3(71)/v4(68) 指另一组变更，同名不同义会静默错配 |
| **D5** | 准入预算闸放 [`OfflineRunner`](../../app/services/inference/offline/runner.py)，**框架层判、策略层报成本**；超预算返 `skipped` 不抛异常；Linux 侧另加 `RLIMIT_AS` 兜底 | OOM 不只是不好看：Linux OOM killer 挑 RSS 最大的进程，同机跑着后端时被杀的很可能是 uvicorn 而非 CLI——这是从「离线分析」通向「在线服务中断」的路径。`skipped` 对齐现有「订阅 source 无特征」口径 | 估算器是模型不是测量，须单测钉在实测点上。**Windows 无 `RLIMIT_AS`，只有一层**，如实记录不假装跨平台 |
| **D6** | `ModelInput.features` 改 `np.ndarray [T,F] float32` | 8.1× 装箱 + 4 次全量转换；且 ROI 提案已论证视觉特征绝不能走这条路（2560 维时约 750 MB），与其长期维持「一个字段能进一个不能进」的双轨约定不如一次改掉 | 影响面限于 `offline/impl/clean.py` + `mock.py` + 测试构造点 |
| **D7** | 在线 `window_seconds` 加**加载期上限**（默认 60 s），可按算子类覆盖 | 对齐仓内 `model_input_fps` 先例：配错不崩、只静默降级的参数设为加载期校验。60 s 依据：现值 10 s / 75 帧 / <12 ms，线性外推 60 s ≈ 70 ms，对 500 ms tick 预算仍 7× 余量 | 上限不是推荐值。**刻意不改** `_advance` 全窗重算为增量推理——那是性能优化，需维护跨 tick 隐状态，与「定边界」正交 |
| **D8** | 显存不变式固化为两条结构断言（`_isolate_cpu` 调用序、`TemporalOperator._device` 恒 CPU），放 [tests/test_import_hygiene.py](../../tests/test_import_hygiene.py) | 该文件已是「结构约束用测试守」的先例 | 对 ROI 提案定前置约束：**encoder 继承 CPU-only 隔离**；确需 GPU 必须显式 opt-in + 声明显存预算 + batch 上限，不接受隐式 `.cuda()` |
| **D9** | `feat/feature-exp` **作废**，按本提案在当前分支重做 | 与 package-layout 重构撞同一目录，合并成本高于重做 | 其实测数据与设计结论作参考实现引用 |
| **D10** | **不跟**废弃分支的另两项特征变更（刷具类别裁剪、`effective_fps` 中位数改均值） | 缩小改动面，保持 `v2` 口径（OBJECTS 集合、impute、fps 估计法全不变，只去时间编码） | 但它们的机会成本在同一次重训里，见遗留风险 |

---

## 三、证据链

所有数字的来源与可复现性：

| 结论 | 数字 | 来源 |
|------|------|------|
| A 内存/时间双平方 | T=500/1000/2000/4000 → 25 MB/1.9 s、100 MB/7.6 s、400 MB/31 s、**1.6 GB/142 s**；增长比 3.97/4.08/**4.60** | 本次实测（`.venv`，合成 `FrameFeature`，5 框/帧、15 fps）。内存严格等于 `20 B × D × T`，与实测逐点吻合 |
| A 的 T≥9000 量级 | T=9000 → ≈8.1 GB / ≥12 min；T=18000 → ≈32 GB / ≥48 min | 按平方外推。尾部增长比 4.60 > 4.00（超缓存后分配器与带宽代价叠加），故**外推值是下界** |
| A 修复收益（合成） | T=4000：1.6 GB/142 s → **0.48 MB/0.11 s**（3300× / 1300×）；T=18000 → 2.16 MB/0.44 s | 本次原型实测；同时对 (T=800, 5 框/帧) 与 (T=1500, 3 框/帧) 两组、9 个目标的 `count`/`slot` 做 `np.array_equal` **全等 PASS** |
| A 修复收益（**真实数据**） | 1886 帧、hand 2661 框（旧实现约 100 MB）→ bbox 特征 **6349.8 ms → 16.0 ms（397×）** | 废弃分支 `a23814b` commit message；与拆前 `np.array_equal` 全等，pytest 465 passed。**这是最强的一行——生产数据密度上测的** |
| B ASFormer O(T²) | T=4500/9000/13500/18000 → 1.7 / 5.4 / 11.6 / 20.3 GB；系数反解 **66.7 B × T²** | ROI 提案实测表。反解自洽：T=4500 代入得 1.35 GB + 约 0.3 GB torch 基线 ≈ 1.65，与实测 1.7 吻合 |
| B 分块后量级 | L=2000 → 267 MB；L=3000 → 600 MB | 由上述系数推算。**内存不构成 chunk 长度的约束**，正确性侧（=训练切片长度）才是 |
| C 装箱倍数 | [4000×113]：ndarray 1.8 MB → 装箱 **14.7 MB（32.6 B/元素，8.1×）** | 本次实测 |
| 线性基线（供预算闸标定） | `FeatureStore.load` 常驻 **2.6 KB/帧**（T=4500/9000/18000 → 11.7/23.5/47.0 MB，严格线性）；MS-TCN 前向 ≈7.7 KB/帧；BiGRU ≈1.8 KB/帧；torch 基线 ≈300 MB | 本次实测 + ROI 提案 |
| T 无上限 | 六个环节逐一核查无 limit；唯一天花板 `task_max_duration: 7200.0` → T ≤ 108,000 → 平方项 ≈1.17 TB | 本次代码核查（见 §1.4 表） |
| A 是回归 | `d4a3f87` 引入、`a23814b` 修复、`feat/feature-exp` 未合回 | `git log -S"_collect_object_arrays" --all` + 各提交 diff |

> 每帧检测框数（上表取 5）是唯一的假设系数。内存严格线性于它：3 框/帧则 10 min 为 4.9 GB，8 框/帧则 13 GB。**结论不随该系数改变**；预算闸直接吃 `detection_count`（load 后已知），不依赖假设。

---

## 四、遗留风险

| 风险 / 待办 | 影响 | 处理 |
|------------|------|------|
| **训练侧须在 checkpoint 写入 `train_slice_frames`** | 不写 → ASFormer 加载期失败（期望行为） | 需与训练侧约定字段名并在重训时落进产物 |
| **D4 是一次特征口径变更** | 113→110，现存全部 checkpoint 失配 | **不是阻塞项**（模型本就要重训），但**必须与重训同批发布**，否则线上三模型一起加载失败 |
| 同批重训可顺带做的清理 | 机会成本在同一次重训里 | 废弃分支实测两项：① 刷具类别现场基本检不出、**33 列恒为零**且让 normalizer std=0；② `effective_fps` 中位数偏低 7~13%（真实 step 13.96/13.28 vs 真值 15.0）。本提案按 D10 不跟，**但若这次重训要动，一起动比分两次便宜** |
| BiLSTM/BiGRU 长 T 分布外 | 内存无虞，但若训练按定长切片，长序列输出同样偏离分布 | 不处理——感受野无限，分块反而改变输出。重训时若切片远短于实际 step，需评估改长序列训练 |
| 成本估算器随代码漂移 | 闸门失准 | 单测钉在 §三 实测点；Linux `RLIMIT_AS` 作第二层 |
| 在线 `_advance` 每 tick 全窗重算 | 非缺陷，是 O(window) 的成本形状 | D7 先用上限挡住；增量化单独排期 |
| ROI encoder 设备归属 | 落地时若隐式上 GPU，与在线 YOLO 抢卡 | D8 已定约束，需在 ROI 提案落地时执行 |

### 落地顺序

1. **A（按帧装桶）+ 等价回归测试** —— P0，与模型权重完全解耦，可立刻推进。单独就能让 10/20 min step 从跑不动变可跑。
2. **D6（ndarray）+ D5（预算闸）** —— 一起落，仍兼容现有 checkpoint（不改列）。
3. ~~**D7（在线上限）+ D8（显存断言）**~~ —— **判定不落**，理由见落地记录 §「不落的两项」。
4. **D4（删三列）+ D2/D3（分块）—— 必须与重训同批。** 次序：约定字段名 → 训练侧按 110 维重训并写入 `train_slice_frames` → 后端代码与新权重同时上。

> 第 1、2 步已于 2026-09-06 落地（见下）；第 4 步是唯一需要跨团队对齐的，仍待重训。

---

## 五、落地记录（2026-09-06）

### 5.1 D1：按帧装桶（P0，已落）

[`offline/impl/clean.py`](../../app/services/inference/offline/impl/clean.py)：
`_collect_object_arrays` → `_collect_object_buckets`，返回类型
`Dict[str, List[ndarray[T,5]]]` → `Dict[str, List[List[ndarray[5]]]]`（外层按帧下标，长度恒 T）。
`_select_hand_slots` / `_select_top1_slot` 改吃桶，删掉每帧重扫整段的
`[_as_box5(arr[t]) for arr in arrs if _as_box5(arr[t])[0] > 0]`。
新增 `_empty_buckets(frames)` 供 `base_feature_names()` 与 T=0 分支复用。

**其余一律没动**：`_box_score` / `_as_box5` / `_impute_short_gaps` / `_missing_age` /
`_build_feature_matrix` 的拼装顺序与 113 个列名全部原样——按 D1 约束，等价论证要短到能一眼看完。

**实测（本机 `.venv`，合成 `FrameFeature`，5 框/帧、15 fps，与 §三 同口径）**：

| T | 旧 collect | 旧 select | 旧内存 | 新 collect | 新 select | 新内存 |
|---|-----------|----------|--------|-----------|----------|--------|
| 1000 | 47.7 ms | 7 769 ms | 95.4 MB | — | — | 0.095 MB |
| 2000 | 156.5 ms | 31 327 ms | 381.5 MB | — | — | 0.191 MB |
| 4000 | 592.1 ms | **127 286 ms** | **1 525.9 MB** | 48.8 ms | 43.3 ms | **0.381 MB** |
| 9000 | — | — | — | 25.7 ms/kf | 21.2 ms/kf | 0.858 MB |
| 18000 | — | — | — | 258.5 ms | 190.6 ms | 1.717 MB |

- 旧实现复现了提案的平方：内存/时间每翻倍 T 都 ×4，T=4000 为 1.53 GB / 127.9 s（提案记 1.6 GB / 142 s）。
- **T=4000：127.9 s → 92 ms（1 389×）、1 525.9 MB → 0.381 MB（4 005×）**。
- 新实现严格线性：T 翻倍 → 时间与字节都恰好翻倍（18000 是 9000 的 2.00×）。
- 整条 `build_base_features`（含 impute / 目标对 / 矩阵拼装）：T=4000 → 208 ms、T=9000 → 476 ms、
  T=18000 → 980 ms，RSS 增量 ≤ 7 MB。**10 / 20 min step 现在都是秒级、十几 MB。**

### 5.2 D6：`ModelInput.features` 改 ndarray（已落）

同文件：`features: List[List[float]]` → `np.ndarray [T,F] float32`，
`@dataclass(frozen=True)` 加 **`eq=False`**（ndarray 字段会让自动生成的 `__eq__` 返回布尔数组、
真值判定即报错；无调用点依赖相等性）。`frame_count` 改读 `shape[0]`；T=0 返回 `zeros((0,113))`
而不是 `[]`，下游不必为空分支特判。`_with_features` 去掉 `.tolist()`；
`add_centered_window_stats` / `add_business_priors` 去掉入口的 `np.asarray` 往返。

**`offline/impl/mock.py` 无需改动**——`BrushRulesSegmenter` 根本不构造 `ModelInput`
（`preprocess` 原样透传 frames）。提案 D6 的「影响面含 mock.py」是估错的，实际无接触面。

### 5.3 D5：准入预算闸（已落）

- **框架层判**：[`offline/runner.py`](../../app/services/inference/offline/runner.py) 新增
  `_check_memory_budget()`，在 `load` 之后、**`preprocess` 之前**判（preprocess 本身就是最大那笔开销，
  判在它之后等于没判）。超预算返回 `OfflineRunResult("skipped", producer, 0, msg)`，
  msg 带 T / detection_count / 估算值 / 预算值 / 怎么调。**不抛异常**，与既有「订阅 source 无特征」同口径。
- **策略层报成本**：[`offline/segmenter.py`](../../app/services/inference/offline/segmenter.py) 基类新增可选
  `estimate_memory_mb(frames) -> Optional[float]`，默认 `None` = 不报成本 → 不拦
  （轻量/规则型策略无需为此写估算）。clean 三个模型各自实现，只吃 `len(frames)` 与检测框总数，
  **不依赖「每帧几个框」的假设系数**。系数与来源写在 clean.py 的 `_TORCH_BASELINE_BYTES` 一段注释里。
- **两处放行**：估算器抛异常 → 放行并记 warning（闸门失准不该让本可跑通的任务失败）；策略不报成本 → 放行。

**预算来源**：[`app/settings.py`](../../app/settings.py) 新增 **`process_memory_budget_mb: int = 4096`**
（env `CLEANSIGHT_PROCESS_MEMORY_BUDGET_MB`）。刻意**命名为通用的单进程 RAM 上限、不与离线链路绑定**——
任何「输入规模由外部决定、可能跑爆内存」的重批处理作业都该在动手前拿自己的成本估算跟它比。
CLI `run` 加 `--memory-budget-mb` 覆盖单次运行。

**估算器在默认 4 GB 预算下的准入形状**（5 框/帧，单位 MB）：

| step 时长 | T | MS-TCN+BiLSTM | ASFormer | BiGRU |
|-----------|---|--------------|----------|-------|
| 2 min | 1 800 | 320 | 513 | 313 |
| 5 min | 4 500 | 350 | **1 606** | 332 |
| 10 min | 9 000 | 401 | **5 488 ✗** | 364 |
| 20 min | 18 000 | 502 | 20 981 ✗ | 429 |
| 120 min（看门狗上限） | 108 000 | 1 511 | 742 676 ✗ | 1 071 |

估算器与 §三 实测点吻合（ASFormer T=4500 → 1 606 MB vs 实测 1.7 GB；T=9000 → 5 488 MB vs 实测 5.4 GB）。
读法：**修完 D1 后，MS-TCN+BiLSTM / BiGRU 连 2 小时的最坏情况都在预算内**（提案预判的「线性下仍约 1.3 GB」）；
**唯一会被拦的是 ASFormer，约 7.5 min 以上的 step 即超预算**——这正是 D2/D3 分块要解决的那一项，
在它落地前被拦下是期望行为，不是缺陷。

**Linux 第二层兜底**：CLI 新增 `_limit_address_space()` 设 `RLIMIT_AS`。两处如实记录：

1. **`RLIMIT_AS` 限的是虚拟地址空间不是 RSS**，torch/BLAS 预留的地址空间远超实际驻留，
   按预算原值设会误杀正常任务。故取 `max(2×预算, 预算+2 GB)`——只挡量级失控，精确的那层是准入闸。
2. **Windows 无 `resource` 模块 → 只有准入闸这一层**，代码里记一行 info，不假装跨平台。
3. rlimit 作用于**调用者进程**，而测试会在 pytest 进程内直接调 `cli.main()`。故
   `main(apply_process_limits=False)` 为默认，只有 `__main__` 真入口显式传 `True`
   （否则在 Linux 上跑测试会把 pytest 进程自己限住）。有用例守这条。

### 5.4 不落的两项

| # | 处置 | 理由 |
|---|------|------|
| **D7** 在线 `window_seconds` 加载期上限 | **不落** | 在线链路未接 ROI 视觉特征，今天没有内存压力，不为一个尚未存在的风险加配置闸。`window_seconds` 配大仍会静默拖垮 tick 相位（`CleanOperator._advance` 每 tick 全窗重算），作**已知风险**留档；真接 ROI 时再评估 |
| **D8** 显存不变式固化为结构断言 | **不落** | **离线推理后续可能要用 GPU**，把「时序侧恒 CPU」钉成测试会挡路。连带地，D8 对 ROI 提案定的「encoder 继承 CPU-only 隔离」这条**跨提案前置约束撤回**——设备归属由 ROI 提案落地时自行决定并声明显存预算（该提案 §「待定：encoder 选型与 CPU 预算」的 CPU-only 表述是它自己的设计判断，不受本文约束） |

§1.1 的两个尾巴随之保留为现状：CPU-only 不变式仍只由 `_isolate_cpu` 的调用顺序与
`TemporalOperator._device` 两行硬编码守着，**无测试**。

### 5.5 测试

| 文件 | 内容 |
|------|------|
| **`tests/test_offline_feature_scale.py`（新增）** | D1 的等价与规模守卫。文件内保留唯一一份旧稠密实现副本（隔离在测试里、不留生产代码）：① `(T=800,5框)` 与 `(T=1500,3框)` 两组、9 类目标逐个 `np.array_equal` 全等；② 整条 `build_base_features` 的 113 维矩阵与「旧收集+旧选槽位」全等；③ 桶占用**恰等于 `20 B × 检测框数`**（确定性断言，不靠计时、不会 flake）；④ ndarray 契约与空输入形状 |
| `tests/test_offline_pipeline.py` | D6 契约（dtype/shape/ndarray）；`TestMemoryEstimator` 把估算器钉在 §三 实测点上（±25% 容差，只挡量级失控）；Runner 四例（超预算 skipped 不落 facts / 调大预算即 completed / 估算器异常放行 / 不报成本不拦）；CLI 两例（`--memory-budget-mb` 直通闸门、`main()` 默认不动本进程 rlimit） |

全量 `pytest tests/` **468 passed**（改动前 451，本次净增 17 例）。

### 5.6 仍未落的

- **D2 / D3（ASFormer 分块 + `train_slice_frames` 随 checkpoint 走）**、**D4（删 `t_norm/t_sin/t_cos`，113→110）**：
  按提案 §落地顺序第 4 步，必须与重训同批。次序不变：约定字段名 → 训练侧按 110 维重训并写入
  `train_slice_frames` → 后端代码与新权重同时上。
- 在此之前，**长 step 走 ASFormer 会被预算闸拦成 `skipped`**（见 §5.3 表），MS-TCN+BiLSTM / BiGRU 不受影响。
- §四「遗留风险」里的其余条目（BiLSTM/BiGRU 长 T 分布外、同批重训可顺带做的清理）状态不变。
