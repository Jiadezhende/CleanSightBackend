# gru-nodep 与 mstcn2-bundle 迁成自包含包，mstcn2-role-embed 契约收进 contract.py

> **变更状态**：生效中（2026-10-17）
> **知识库**：已沉淀 → [SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)（2026-10-17）

## 概述

新增 `impl/clean_gru_nodep/`（`CleanGRUNodepSegmenter`，原 `CleanNodepGRUSegmenter` 按「架构在前、特征在后」改名）与
`impl/clean_mstcn2_bundle/`（`CleanMSTCN2BundleSegmenter`，原只在未合入分支上的 v5-bundle）。mstcn2-role-embed 的物料文件名、
输入分辨率、网络结构从 segmenter.py / model.py 收进 `contract.py`。旧 `clean.py` 与 YAML 本批不动。

## 变更背景

- **承接**：[20261017_OFFLINE_LEGACY_V2_PACKAGES.md](20261017_OFFLINE_LEGACY_V2_PACKAGES.md)（同一套包版式与按帧分桶改法）。
- **mstcn2-bundle 的来源**：分支 `feature/offline-v5-bundle-mstcn2` 的 3454d94 给每条检测分配稠密数组，读框与选槽位随帧数平方增长（人定不沿用）。
  它的 94 维特征正好是 mstcn2-role-embed 交付包 `bundle.bundle_columns` 的输出（base 83 + axis 2 + pair 9），那份实现已按帧分桶提速。
- **改名的影响**：producer（= 类名）只用于展示与 CLI 过滤；Runner 重跑删掉该 run 全部旧分段、不按 producer 过滤，不影响幂等。
  已落盘的旧 run 仍显示 `CleanNodepGRUSegmenter`，重跑后变新名。

## 方案详情

### 全景

```text
impl/clean_gru_nodep/      contract（LABELS 6 类 / 226 维 / WINDOW 16 / STRUCTURE hidden 128 × 3 层 / model.pt 训练状态格式）
                           features：旧 v2 ⊕ v3 拼接原样拷贝（按帧分桶） → resample_by_ts(7.5, strict) → 因果滑窗 GRU → 丢短段解码
impl/clean_mstcn2_bundle/  contract（6 类 / 94 列 = 训练侧 meta.json 列序 / STRUCTURE in_dim 94 / model.pt 训练状态格式）
                           features_ama + bundle + frames.detection_rows：role-embed 包原样拷贝
                           → 低于 15fps 只告警 → 整段 MS-TCN2（只取 model_state）→ pp20
impl/clean_mstcn2_role_embed/contract.py  += STRUCTURE / WIDTH, HEIGHT / TEMPORAL_FILE / HAND_STATE_FILE
```

| checkpoint 格式 | 包 | 加载 |
|----------------|----|------|
| 训练状态 `{schema_version, checkpoint_kind, epoch, model_state, optimizer_state, …}` | gru_nodep / mstcn2_bundle | `weights_only=True`，只取 `model_state`，strict |
| 纯 state_dict | mstcn2_role_embed | `weights_only=True`，strict |

## 变更效果

| 项 | 结果 |
|----|------|
| gru-nodep 逐位核对（一次性脚本不入库）：旧类 vs 新包，同一份随机 state_dict（本机无该权重），5 组合成序列 × `confidence_override` ∈ {1.0, None} | 特征、`label_probs`、分段全部逐位一致；600 帧特征 1.49 s → 0.07 s |
| mstcn2-bundle：示例视频 c5d477f3 框文件 → 本包 94 维 vs 训练侧 `ama-v5-bundle-94d` 特征 | **逐位一致**，列序与 `meta.json` 相同；用本机权重前向的概率与「训练特征前向」逐位一致 |
| mstcn2-role-embed 契约挪动后 | 特征与训练逐位一致；真实 Runner 全链路 5 段与挪动前相同，缓存命中、重跑 0.7 s |
| 新增用例 | gru-nodep：降采样 + 226 维 + 解码、strict 拒低帧率、因果窗口、缺权重先于 torch 报错、参数校验、物料；mstcn2-bundle：列序、pp20、低帧率只告警、缺权重、物料 |
| 全量 `pytest tests/` | 1116 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| gru-nodep 本机无真实权重，核对用的是随机 state_dict | 加载格式按旧代码同口径（`model_state`），真实权重未在新包上跑过 | 迁物料批次在有权重的机器上跑一次 `cli materials` + 离线作业 |
| mstcn2-bundle 权重 fold0-seed42 只见过 4/5 训练视频 | 研究对照用，非上线默认 | 无 |
