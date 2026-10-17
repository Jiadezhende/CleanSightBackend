# v2 系三个历史离线策略迁成自包含包（MS-TCN+BiLSTM / ASFormer / BiGRU），特征提速约 45 倍

> **变更状态**：生效中（2026-10-17）
> **知识库**：待沉淀

## 概述

新增 `impl/clean_mstcn_bilstm/`、`impl/clean_asformer/`、`impl/clean_bigru/`，各自一份 v2 特征、网络、加载与解码，互不 import。
类名与旧 `impl/clean.py` 相同（producer 不变），构造参数 `model_path` 换成 `model_dir`（物料 `model_dir/model.pt`）。
新包与旧类在合成序列上特征 / 概率 / 分段逐位一致；检测归拢改为按帧分桶，特征计算从随帧数平方增长变为线性。
旧 `clean.py` 与 YAML 本批不动，调用点在后续批次迁移。

## 变更背景

- **承接**：[20261017_OFFLINE_SEGMENTER_FRAMEWORK.md](20261017_OFFLINE_SEGMENTER_FRAMEWORK.md)（策略包隔离门禁、`materials()`）。
- **现状**：三个策略在 `clean.py` 里共用 `_CleanTorchSegmenter` 与 v2 特征函数。v2 读入层给**每条检测**分配一个 `[T,5]`
  稠密数组，选槽位时每帧遍历全部数组：实测 BiGRU 特征 1800 帧 39.9 s，10 分钟 step（7.5fps 约 4500 帧、约 3.6 万条检测）
  内存约 3 GB、耗时以十分钟计。

## 方案详情

### 全景

```text
impl/clean_<模型>/
  __init__.py   来源与改动点
  contract.py   LABELS（6 类旧类序）、FEATURE_VERSION / FEATURE_DIM、MODEL_FILE = "model.pt"、checkpoint 格式说明
  features.py   v2 基础 113 维（+ 本模型 recipe）→ build_features(frames, fps, w, h)
  model.py      build_model(in_dim, class_count)：旧 _make_<模型> 原样
  segmenter.py  <旧同名类>：基类 _CleanTorchSegmenter 摊平（加载 / 归一化前向 / 丢短段解码），materials() = (model.pt,)
```

| 包 | 特征 recipe | 维度 | 原物料 |
|----|------------|------|--------|
| `clean_mstcn_bilstm` | v2 | 113 | `clean-offline-mstcn.pt` |
| `clean_asformer` | v2 + business_priors | 121 | `clean-offline-asformer.pt` |
| `clean_bigru` | v2 + center_window + business_priors | 249 | `clean-offline-bigru.pt` |

### 1. 唯一的实现改动：检测按帧分桶

`_collect_object_arrays` 由「每条检测一个 `[T,5]` 数组」改为 `{帧号: [每条检测一行 [5]]}`；`_select_hand_slots` / `_select_top1_slot`
只看本帧桶。旧实现在第 t 帧筛出的候选，正是第 t 帧追加的那些行、且按追加顺序——与新桶内顺序相同，排序与打分不变，所以数值逐位一致。
与 mstcn2-role-embed 交付包对 `clean_bbox_v2` 做的提速是同一个改法。

### 2. 加载与校验

checkpoint 是带元数据的训练导出（`state_dict` + `class_names` / `feature_names` / `feature_version` / numpy normalizer）。
原样加载（`weights_only=False`，存量例外），在原有 feature_names / feature_version 校验之外**新增 class_names 与契约类序的校验**；
权重文件不存在时在 import torch 之前就报 `FileNotFoundError`。

## 变更效果

| 项 | 结果 |
|----|------|
| 逐位核对（一次性脚本不入库）：3 个模型 × 5 组合成序列（多类多框+缺口、周期空帧、稀疏、全空、单帧），旧类配真实权重 vs 新包 | 特征、列名、ts、fps、`label_probs`、分段（含 conf / meta）**全部逐位一致** |
| 特征耗时 | 400 帧：旧 1.9 s → 新 0.04 s；新包 9000 帧 1.3 s（旧 1800 帧 39.9 s） |
| 新增用例 | 每包：特征口径、分段解码 + label_probs、短段丢弃、缺权重先于 torch 报错、物料清单 |
| 全量 `pytest tests/` | 1102 passed |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| YAML / 物料仍指旧 `clean.py` 与扁平文件 | 无（本批未切换） | 迁调用点批次统一切换 |
| v2 特征含视频内相对时间列（t_norm / t_sin / t_cos） | 历史模型的既有口径 | 不改：历史策略按原样保留 |
