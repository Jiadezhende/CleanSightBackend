# CLEAN 离线分割默认模型改为 BiGRU，三份离线权重统一改名

> **变更状态**：生效中（2026-09-25）
> **知识库**：待沉淀

## 概述

`config/inference_config.yaml` 的 CLEAN（stage `2`）`offline.class` 由 `CleanMSTCNBiLSTMSegmenter` 改为 `CleanBiGRUSegmenter`，
`model_path` 指向 `clean-offline-bigru.pt`。随训练仓交付的三份离线权重按 `clean-offline-<模型>.pt` 改名。只动配置与权重文件名，代码不变。

## 变更背景

- **现状**：配置指向的 `clean-offline-mstcn-bilstm.pt` 不存在，CLEAN 离线运行报 `FileNotFoundError`。训练仓交付了三份权重
  `best_{ms_tcn,asformer,bigru}_offline_segmenter.pt`，名字与本仓 `clean-large-best.pt` 等不成体系。
- **选型依据**（checkpoint 自带的逐帧验证指标）：

| 权重 | 后端类 | 特征 | 训练方式 | 逐帧 accuracy |
|------|--------|------|---------|--------------|
| `clean-offline-mstcn.pt` | `CleanMSTCNBiLSTMSegmenter` | 113 维 | full_sequence | 0.24（从不预测 idle / 短刷 / 冲洗） |
| `clean-offline-asformer.pt` | `CleanASFormerSegmenter` | 121 维 | full_sequence | 0.65 |
| `clean-offline-bigru.pt` | `CleanBiGRUSegmenter` | 249 维 | sliding_window | **0.75** |

三份都是 3 epoch、10 段训练序列；验证集无 `air_injection` 样本。

## 方案详情

| 部件 | 改动 |
|------|------|
| `config/inference_config.yaml` | CLEAN `offline.class` → `CleanBiGRUSegmenter`，`model_path` → `clean-offline-bigru.pt`；注释列出另两个类与权重的对应 |
| `app/data/`（不进 git） | `best_bigru_offline_segmenter.pt` → `clean-offline-bigru.pt`，`best_asformer_...` → `clean-offline-asformer.pt`，`best_ms_tcn_...` → `clean-offline-mstcn.pt` |
| `README.md` | 默认离线模型一句同步 |

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 权重兼容性 | 三份均：`feature_names` 与后端逐列相等、`feature_version` 一致、`class_names` 与 `ACTION_LABELS` 同序、`strict=True` 加载 + 前向输出 `[T, 6]` 且行和为 1 |
| CLI 回环 | 临时存储根铺 60s @ 15fps 合成检测，`cli run --task-id 9 --step-id 2`（真实配置）→ `completed producer=CleanBiGRUSegmenter segment_count=3`，落 `temporal.jsonl` + `label_probs.npz`（900×6） |
| 全量 `pytest tests/` | 900 passed（测试用注入配置，不读本 YAML） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| BiGRU 以 sliding_window 训练、后端整段推理 | 若训练仓的 `t_norm / t_sin / t_cos` 按窗口归一化，推理时这三列语义偏移，结果静默变差 | 核对训练仓特征生成是按窗口还是按整段；不一致则改用 ASFormer 或对齐推理切窗 |
| 三份权重均为少量数据的早期版本 | 分割质量有限，`air_injection` 未验证 | 训练仓出正式权重后按同名替换 |
| 部署机需放 `clean-offline-bigru.pt` | 缺失则 CLEAN 离线作业失败（不影响在线） | 随模型物料分发 |
