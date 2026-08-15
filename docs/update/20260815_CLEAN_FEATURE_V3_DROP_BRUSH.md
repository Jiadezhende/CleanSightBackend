# CLEAN 离线特征清理：移除检不出的刷具类别（v2 113 维 → v3 71 维）

> **变更状态**：生效中（2026-08-15）
> **知识库**：待沉淀（需同步修正 [SERVICE_INFERENCE](../kb/SERVICE_INFERENCE.md) 与 [ARCHITECTURE_STORAGE_AND_SCHEMA](../kb/ARCHITECTURE_STORAGE_AND_SCHEMA.md) 里的旧维度数字）
>
> 承接：[离线导出器接入视觉分支](20260815_OFFLINE_EXPORT_VISUAL_R1.md)。在真实数据上跑导出时发现 113 维里有 45 维恒为零，追下去是特征契约与实际检测能力脱节。

## 概述

- **改了什么**：CLEAN 离线特征把 `short_brush` / `long_brush` / `brush_tip_out` 三个目标、涉及它们的 5 组关系特征、以及 6 维基于它们的业务先验全部移除。基础维度 **113 → 71**，`+business_priors` 121 → 73，`+center_window+business_priors` 249 → 151。`FEATURE_VERSION` 随之 `clean_bbox_v2_top1_impute` → `clean_bbox_v3_detectable`。
- **为什么改**：刷具这类细长、高度遮挡的目标现场基本检不出，两个部署检测器（`clean-large-best` / `clean-small-best`）的 `names` 里根本没有这些类别。它们在特征里的唯一效果是 **45 列恒为零**——白占输入维度、让 normalizer 的 std=0、并在训练时提供纯噪声维度。
- **影响面**：只改 [offline/impl/clean.py](../../app/services/inference/offline/impl/clean.py) 与两个测试文件。**不改在线链路**（`CleanOperator` / `gru-final.pt` 是独立的实时特征管线，其 `objects` 映射另有 8 槽位，不在本次范围）。

## 这不是新决策，是把代码补齐到既定决策

[CLEAN 模型提案 §3.2D](20260814_CLEAN_STAGE_MODEL_PROPOSAL.md) 已明确：

> 明确不把 `short_brush`、`long_brush`、`brush_tip_out` 作为输入检测类别。动作名可以保留"长刷/短刷"的业务语义，但模型证据来自手部局部画面、可见的大目标及跨帧变化，不来自刷具检测框。

代码此前仍把它们列在 `OBJECTS` 里，于是产生了整整 45 列恒零特征。本次只是让特征契约与该决策、与检测器实际能力对齐。

**长/短刷动作的识别路径不受影响**，反而更清楚：证据来自手部与可见大目标的相对运动 + 手部 ROI 局部画面。实测支持这条路径可行——`database/99/2` 428 帧样本里 **hand 出现率 93.0%、scope_mid_section 92.8%、两者同时可见 90.2%**，最长连续缺手仅 1.5s。

## 改动详情

### 1. `OBJECTS` / `OBJECT_ALIASES` / `PAIR_FEATURES`

只保留两个部署 checkpoint 的 `names` 真会产出的 6 类：

```
clean-large-best → hand / scope_control_body / scope_mid_section
clean-small-best → syringe / air_gun / scope_distal_end
```

`OBJECT_ALIASES` 随之退化为恒等映射（`{name: name}`），不再维护第二份清单。

关系特征 7 组 → 2 组，移除的 5 组两端都含刷具：

| 移除 | 保留 |
|---|---|
| `hand ↔ short_brush` | `air_gun ↔ scope_distal_end` |
| `hand ↔ long_brush` | `syringe ↔ scope_distal_end` |
| `brush_tip_out ↔ scope_distal_end` | |
| `short_brush ↔ scope_control_body` | |
| `long_brush ↔ scope_mid_section` | |

### 2. `add_business_priors`：8 维 → 2 维

6 维长/短刷先验（`prior_short_clean_near` / `prior_short_clean_motion` / `prior_long_signal_near_scope` / `prior_long_towards_distal` / `prior_long_away_distal` / `prior_hand_long_contact`）全部建立在刷具列上，恒为零。保留 `prior_flush_stable` / `prior_air_stable`——两端目标都真能检出。

### 3. 测试

`tests/test_offline_pipeline.py` 的 `_clean_frame` 原本用 `short_brush` 造帧，改用 `syringe`：用检不出的类别造测试数据，只会得到一堆恒零列，测不出任何东西。

## 对既有 checkpoint 的影响

**无实际影响**：`app/data/` 下不存在离线 CLEAN checkpoint，`config/inference_config.yaml` 里两个生产 stage 的 `offline` 均为 `{}`（未启用）。

若日后加载按 v2 训练的旧权重，[`_load_model`](../../app/services/inference/offline/impl/clean.py) 会因 `feature_names` / `feature_version` 不一致而**硬失败**——这是正确行为，好过静默喂进 42 列错位输入。

## 验证

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | **453 passed**，无回归 |
| 维度 | base 113→71、+priors 121→73、+window+priors 249→151 |
| 真实数据恒零列 | `database/99/2` 428 帧：**结构性恒零 45 → 0** |
| 三份样例重新导出 | `r0@none` (428,71) / `r1@yolo` (428,328) / `r1@resnet18` (428,584)，均正常 |

> 清理后仍有 6 列在该样本里恒零，是 `air_gun` / `syringe` 的关系特征——这两个目标在这段视频里出现率仅 0.2%（该片段不含灌注/注气），属**数据相关**而非结构缺陷，在含相应动作的片段里是活的。

## 后续计划

1. R2：手部 ROI 细粒度池化（stride-8 浅层 RoIAlign，K=2 覆盖 99.7% 帧）——这是替代刷具检测的主路径，不是收尾项；
2. 下次 KB 聚合时修正 `SERVICE_INFERENCE.md` 与 `ARCHITECTURE_STORAGE_AND_SCHEMA.md` 里的 113/121/249 三处旧数字。
