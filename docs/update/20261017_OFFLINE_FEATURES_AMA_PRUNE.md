# mstcn2-role-embed / mstcn2-bundle 的 features_ama 删去跑不到的代码

> **变更状态**：生效中（2026-10-17）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)（2026-10-17）

## 概述

两个包里同一份 `features_ama/`（从训练仓 `tools/features_ama` 复制的通用特征库）只保留 bundle 94 列实际用到的路径：
删除 `clean_bbox_v4.py`（只剩的 `V4_PAIRS` 并入 v5 的 `COMPACT_PAIRS`）、v2 的 business_priors / center_window recipe 与 recipe 分派、
v3 / v5 的整段 builder 与列名函数、各版本常量，以及 role-embed 未用的 `roles.ROLE_COLUMNS`。调用结构不变，约 360 行。
两个包的 `features_ama/` 仍逐字节相同。清理计划的后两批（bundle 单遍计算、role-embed 复用中间量）另起记录。

## 变更背景

- 两个模型的特征口径已核对：mstcn2-bundle 94 维（含 v2 帧差速度 7 列与时间位置 3 列，不含 v5 沿轴速度）；mstcn2-role-embed 106 维
  与它共用 `bundle_columns` 这一层，手槽位 26 列同名但按握镜手 / 送刷手重算，另有送刷手速度、手部状态、角色列。两包共用部分的清理完全相同。
- 代价：`features_ama/` 不再与训练仓逐字节一致；文件名与函数名沿用训练仓，`__init__` 写明裁剪来源（extractor git_head 642d74f）。

## 变更效果

| 项 | 结果 |
|----|------|
| mstcn2-bundle：数据集 43 个视频框文件 → 94 维 vs 训练侧 `ama-v5-bundle-94d` 特征 | 全部逐位一致 |
| mstcn2-role-embed：c5d477f3 交付嵌入 → 106 维 vs `expected_features.npy`；43 个视频 + 固定种子伪手部概率、7 组合成边界序列 vs 清理前实现 | 全部逐位一致 |
| `features_ama` 行数 | 1356 → 914 |
| 全量 `pytest tests/` | 1117 passed |

## 遗留风险 / 后续任务

无（数值不变）。
