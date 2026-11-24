# CLEAN 新增 857 维 DINOv2 离线分割策略

> **变更状态**：生效中（2026-11-24，可按 stage 配置启用）
> **知识库**：待沉淀

## 概述

新增 `CleanDINOv2ConcatSegmenter`，复现训练侧的 857 维五类动作分割基线。启用时将 CLEAN 的 `offline` 块替换为策略文件中的 YAML。 [策略配置](../../app/services/inference/offline/segmentation/impl/clean_dinov2_concat/segmenter.py)

## 变更背景

训练侧基线将同帧手部的 DINOv2 嵌入均值直接拼接到几何特征。将这套基线接入后端后，可使用正常的离线作业入口回看结果。

## 方案详情

```text
帧检测 → 89 维几何 + 同帧手框 → raw 裁剪 → 768 维手部嵌入均值
       → 857 维时序输入 → MS-TCN2 → 原始概率、pp5 动作段
```

| 环节 | 实现 |
|---|---|
| 输入 | 640×480。以 15 fps 为训练帧率进行抽稀，低帧率告警后继续推理。同帧手框走面积前两名，按训练口径裁剪、遮住时间戳水印并编码。 |
| 特征 | 原 94 维几何特征去掉三列相对时间位置和两列手槽位单帧速度，保留其余列序。视觉特征按同帧手框在 float32 上取均值，无手帧填零。 |
| 缓存 | `cache_dinov2_base_857d_hand_embeddings.npz` 按手框、时间戳和骨干指纹核对。有手框却取不到对应 raw 帧时，作业报错。 |
| 输出 | MS-TCN2 为 2 个 stage、每 stage 5 层、hidden 32。类序为 idle、flush、long_brush_insert、long_brush_withdraw、short_brush_cleaning。原始五类概率保存到 `label_probs.npz`，以 pp5 归并并跳过 idle 后写入 `temporal.jsonl`。 |

### 物料

- `app/data/clean-offline-dinov2-concat/model.pt` 来自 `Cleansight_models` 的 `outputs/dino-v41-s1-five-class/runs/dinov2/fold0/seed1/mstcn2-20261123-160402/checkpoints/last.pt`，为第 30 轮 checkpoint。SHA-256 为 `496a7816c6a9c442b296c0c118e6e7af8218593fe0926ae7b27e7927216892cb`。严格加载其中的 `model_state`，包括训练集归一化统计。
- 骨干复用 `app/data/backbones/dinov2_vits14/` 的代码和权重。骨干权重 SHA-256 为 `b938bf1bc15cd2ec0feacfe3a1bb553fe8ea9ca46a7e1d8d00217f29aef60cd9`。部署时随物料分发。

## 变更效果

- 迁移对照使用 fold0 的 9 段留出录像的框与训练侧手部嵌入缓存。经新策略重建的 857 维输入逐值一致，五类概率最大绝对差为 0.00039884，逐帧类别和 pp5 分段完全一致。
- 本地 `OfflineRunner` 使用 135 帧样本、真实 raw HLS 解码和 CUDA DINOv2 前向，生成了手部嵌入缓存、135×5 概率和一条由新策略产出的动作段。
- 全量单元与组件测试通过，共 1286 项。

## 遗留风险 / 后续任务

- 当前物料是 fold0 / seed1 权重。模型效果评估使用专用测试集。
