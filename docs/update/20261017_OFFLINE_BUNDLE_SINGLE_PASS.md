# bundle 94 列改为单遍计算，mstcn2-bundle 特征提速约 3.5 倍

> **变更状态**：生效中（2026-10-17）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)（2026-10-17）

## 概述

mstcn2-role-embed 与 mstcn2-bundle 共用的 `bundle_columns` 不再把训练仓的 `full` 与 `fix-compact-pair` 两个 recipe 各算一遍再拼，
改由 `features_ama/clean_bbox_v5.build_bundle` 一遍算出 94 列，并返回中间量（`features` / `active` / 器械轴 `frame`）。
v3 只剩槽位通道投影 `_slot_channels`；v5 的 recipe 分派、速度块、`_scope_block` 删除。两包 `features_ama/` 仍逐字节相同。

## 变更背景

- **承接**：[20261017_OFFLINE_FEATURES_AMA_PRUNE.md](20261017_OFFLINE_FEATURES_AMA_PRUNE.md)。
- **原流程的重复**：读框 3 次、选槽与插值 3 次、v2 矩阵 2 次、v3 整段 113 维矩阵 2 次（其槽位通道随即被 v5 器械轴覆盖，其余列被 `compact_keep` 丢弃）、
  v5 沿轴速度 6 列（`bundle` 全部丢弃）。

## 方案详情

```text
build_bundle(rows)
  arrays = _read_object_arrays(...)                         一次
  abs    = v2._build_base_matrix → compact_keep 留列          一次
  features, active = _object_tracks → frame = scope_frame_offline(smooth=True)
  scope  = 每个槽位 _slot_channels，只留 compact_keep 的几何通道
  axis   = observed / age；pair = _pair_block(features, active)
  每列 nan_to_num(float32)                                   与原 recipe 末尾的整体 nan_to_num 等价（各块原本即 float32）
```

数值不变的依据：原 94 列里 abs 取自 `full` 的 v2 矩阵；scope 只留 v5 器械轴重算的几何通道；pair 只依赖与平滑无关的 `features / active`，
`fix-compact-pair` 那一遍算出的 pair 与在 `full` 这一遍里算的相同。

## 变更效果

| 项 | 结果 |
|----|------|
| mstcn2-bundle：43 个视频 → 94 维 vs 训练侧 `ama-v5-bundle-94d` | 全部逐位一致 |
| mstcn2-role-embed：示例 vs `expected_features.npy`、43 个视频 + 7 组合成序列 vs 清理前实现 | 全部逐位一致 |
| 43 个视频特征耗时合计 | bundle 9.0 s → 2.6 s；role-embed 16.7 s → 10.5 s |
| `features_ama` 行数 | 914 → 632 |
| 全量 `pytest tests/` | 1117 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| role-embed 的 `slots.SlotContext` 仍对 ctrl / mid 另算一遍器械轴 | 重复计算 | 下一批改为复用 `build_bundle` 的中间量 |
