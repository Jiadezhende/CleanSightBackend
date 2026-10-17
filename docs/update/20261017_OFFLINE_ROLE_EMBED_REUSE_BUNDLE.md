# mstcn2-role-embed 复用 bundle 中间量，清掉后端自写部分的死参数

> **变更状态**：生效中（2026-10-17）
> **知识库**：待沉淀

## 概述

`slots.SlotContext` 不再自己对操作部 / 插入部选槽、插值、建器械轴，改为接收同一视频 `compute_bundle` 的中间量；
`hand_rows` 在 segmenter 里只算一次，传给裁剪与特征拼装；删去 `build_features(return_roles=)` 与 `columns=`、
`load_model(device=)`、`predict_probs` 的多模型平均（两个 MS-TCN2 包同改为单模型 `predict_probs(model, feats)`）。

## 变更背景

- **承接**：[20261017_OFFLINE_BUNDLE_SINGLE_PASS.md](20261017_OFFLINE_BUNDLE_SINGLE_PASS.md)（`build_bundle` 已返回 `features / active / frame`）。
- **为什么能复用**：`SlotContext` 原有的 `_top1_slot` / `_cxcya` 与 bundle 读入层的 v2 选槽同口径——float32 化的 (cx, cy, area)、
  打分 `√area − 0.15·位移`、稳定排序取第一，再同样 `_impute_short_gaps(15fps)` 与 `scope_frame_offline(smooth=True)`。

## 变更效果

| 项 | 结果 |
|----|------|
| mstcn2-role-embed：示例 106 维 vs `expected_features.npy`；43 个视频 + 7 组合成序列 vs 清理前实现 | 全部逐位一致 |
| mstcn2-bundle：43 个视频 vs 训练侧 94 维；本机权重前向 vs 训练特征前向 | 逐位一致 |
| 真实 Runner 全链路（示例视频写 raw HLS） | 5 段与清理前相同，嵌入缓存命中，重跑 0.5 s |
| 三批合计（43 个视频特征耗时） | mstcn2-bundle 9.6 s → 2.7 s；mstcn2-role-embed 18.1 s → 10.8 s |
| 全量 `pytest tests/` | 1117 passed |

## 遗留风险 / 后续任务

无。
