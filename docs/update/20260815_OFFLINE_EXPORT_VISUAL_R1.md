# 离线导出器接入视觉分支：帧源 + backbone + R1（不阻塞训练侧）

> **变更状态**：生效中（2026-08-15）
> **知识库**：待沉淀
>
> 承接：[raw 帧索引 sidecar + 离线特征导出器骨架](20260815_RAW_FRAME_INDEX_AND_OFFLINE_EXPORT.md)。那一篇把管道立住并打通 R0（bbox-only），本篇接上视觉分支，产出**带视觉特征的模型输入样例**，使训练侧不必等实验平台落地即可开工。需求定稿见[离线特征融合实验需求](20260814_OFFLINE_FUSION_EXPERIMENT_REQUIREMENTS.md)。

## 概述

- **改了什么**：`offline/export/` 新增帧源与 backbone 两块，打通 R1（R0 + 全帧 CNN 深层全局池化）。R1a/R1b **是同一个 recipe**，只差 `--backbone yolo` / `--backbone resnet18`。
- **为什么改**：R0 只是既有 113 维 bbox 特征，训练仓早已熟悉；真正需要先看到形态的是**融合后的输入**。先把样例产出来，后续实验不被实验平台/可视化阻塞。
- **影响面**：只在 `app/services/inference/offline/export/` 内新增两个模块 + `impl/clean.py` 加一个 recipe。不改在线链路、不改落盘、不改配置。

## 改动详情

### 1. `export/frame_source.py`（新增）— 由 ts 精确取回像素帧

`ts → 定位 raw 段 → 读 sidecar 二分得 ordinal → 顺序解码取第 ordinal 帧`。两条硬约束都来自实测：

> **必须逐段解码，绝不能把多段拼成一条 playlist。** 实测 `database/99/2` 三段（300/300/257 帧）拼成一条 playlist 用 ffmpeg 解只出 **600 帧**——第三段被整段静默丢弃；逐段单独解码则 300/300/257 全对。成因是段间时基/tfdt 不一致（属 [HLS 时基修复](20260813_HLS_SEGMENT_TIMESCALE_FIX.md) 那条线的既有问题，与 sidecar 无关——ffmpeg 不读 `.json`）。在它修好之前，拼接解码是**正确性问题**而非性能问题。

> **按段流式产出，不驻留整段像素。** 428 帧 @640×480 的 BGR 已是 394MB；10 分钟 step 更大。故解一段 → 前向一批 → 只留降维结果。

取不到的帧按原因分项计数（`no_sidecar` / `not_in_playlist` / `no_segment` / `decode_short`）并显式 mask，**绝不退化成按 `eff_fps` 近似反推**——实测那样会让 38.8% 的帧错位（见下）。

### 2. `export/backbone.py`（新增）— 单次前向吐深浅两层

```
raw 帧 ──> 前向一次 ├── 浅层 stride-8  ──RoIAlign(手框)──> 手部细节（R2 用，第 4 阶）
                    └── 深层 stride-32 ──全局池化───────> 场景上下文（R1 用）
```

支持 `yolo`（仓库自有 checkpoint，零下载）/ `yolo:<路径>` / `resnet18`。YOLOv8 主干在 `model.model` 里是纯顺序段（各层 `f=-1`），故 `[:5]` 输出即 stride-8、`[:10]` 即 stride-32，一趟前向两层都留。

### 3. `export/models.py` — `VisualFrames` 改为只带降维结果

原设计让它扛 `deep`/`shallow` 原始特征图，**扛不住**：浅层图是 `T×128×60×80` float32，428 帧就超 1GB。改为 `global_vec` / `hand_tokens` / `hand_mask`——池化与 RoIAlign 都在 backbone 前向的同一趟里做完。

### 4. `impl/clean.py` — 新增 `export_r1`

特征列布局：`[基础 113 维 | visual_global_0..C-1 | visual_valid]`。

- 取不到像素的帧视觉块置零，**语义由末列 `visual_valid` 承载**——零值本身不表达"画面里什么都没有"（不变式 F4）；
- 视觉向量保持 backbone 原始尺度**不做归一化**：归一化统计量属训练侧，落在这里会与训练仓的 normalizer 形成两份真源；
- 缺 `visual` 时硬失败，**不静默退化成 R0**（否则 R1 vs R0 的对照会变成自己比自己）。

## 产出的样例

`database/99/2`（428 帧，30fps raw × 3 段）：

| 样例 | shape | 大小 | feature_version |
|---|---|---|---|
| `r0@none` | (428, 113) | 31 KB | `clean_bbox_v2_top1_impute` |
| `r1@yolo` | (428, 370) | 421 KB | `…+visual_global@yolo:clean-large-best` |
| `r1@resnet18` | (428, 626) | 796 KB | `…+visual_global@resnet18` |

三份的**前 113 列逐值相等**——融合是纯追加，R1 相对 R0 的增益可直接归因到视觉块。

## 给训练侧的两条实测结论

**① 视觉块必须先去均值/标准化，否则信号会被共性分量淹没。**

| backbone | 处理 | 相邻帧余弦 | 随机帧对 | 判别间隙 |
|---|---|---|---|---|
| YOLO 主干 | 原始 | 0.9988 | 0.9890 | +0.0098 |
| YOLO 主干 | **去均值** | 0.8491 | 0.0347 | **+0.8144** |
| ResNet18 | 原始 | 0.9963 | 0.9828 | +0.0136 |
| ResNet18 | **去均值** | 0.7611 | 0.0102 | **+0.7509** |

原始余弦全在 0.99 以上并非特征无判别力，而是全局平均池化的共性分量占绝对主导；去均值后判别间隙从 0.01 跳到 0.81。既有 checkpoint 加载路径本就支持 `normalizer_mean/std`，训练侧照常出 normalizer 即可。

**② 两个 backbone 的信息结构本质不同**（PCA 能量占比）：

| backbone | top1 | top10 | top50 |
|---|---|---|---|
| YOLO 主干 | **43%** | 84% | 98% |
| ResNet18 | 18% | 57% | 86% |

YOLO 主干特征高度集中、有效维度低——恰好印证「它与 bbox 同源，增量只是检测头丢掉的那部分」；ResNet18 明显更分散，携带更多独立信息。这正是 R1a/R1b 对照要测的核心变量。

另：逐帧特征范数与 `hand_count` 的相关系数为 −0.72（YOLO）/ −0.81（ResNet18），说明视觉特征确实跟着画面内容动，不是噪声。

## 验证

| 项 | 结果 |
|----|------|
| 全量 `pytest tests/` | **453 passed**（新增 1 例，无回归） |
| R1a 端到端 | `database/99/2` 428 帧，**取帧 428/428 全部命中**（`no_sidecar=0 not_in_playlist=0 no_segment=0 decode_short=0`），20.5s 墙钟（CPU 8 线程） |
| R1b 端到端 | 同上，38.6s；ResNet18 ImageNet 权重自动下载成功 |
| 融合纯追加 | 三份样例前 113 列逐值相等 |
| sidecar 精确性 | 428/428 精确命中；若走 `eff_fps` 近似则 **38.8% 的帧错位**（165 帧偏 1、1 帧偏 2） |
| 逐段解码必要性 | 逐段 300/300/257 全对；拼 playlist 只出 600（丢整段） |

## 后续计划

1. R2：手部 RoIAlign token，从 stride-8 浅层取，K=2（实测覆盖 99.7% 帧）；
2. 实验平台：lab 页面改名 + 离线实验区 + 清洗时间线可视化；
3. 实验有结论后再写融合 Segmenter（recipe 已就位，只需加载 checkpoint + 解码分段）。

> 提醒：视觉分支依赖 sidecar，只对 sidecar 落地后新产生的数据有效。对旧 step 跑 R1 会硬失败并报出分项原因，不会产出全零视觉样例。
