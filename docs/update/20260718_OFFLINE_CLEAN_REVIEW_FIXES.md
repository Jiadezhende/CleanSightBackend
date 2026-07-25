# 2026-07-18 CLEAN 离线模型 Review 修正

> **变更状态**：生效中（历史记录，本次维护确认）
> **知识库**：已沉淀 → [kb/SERVICE_INFERENCE.md](../kb/SERVICE_INFERENCE.md)(2026-07-21)

## 背景

本次更新基于 PR `feat/offline-infer -> dev` 中对 `app/services/inference/offline/segmenters/clean.py` 的 review 意见，目标是继续收敛 CLEAN 离线模型接入边界：

- CLEAN 离线模型只负责真实 `.pt` 权重推理；
- 无权重回环由已有 MOCK stage / `mock.BrushRulesSegmenter` 承担；
- 特征转换保持尺寸稳定，同时避免 NaN/inf 进入 torch；
- 各具体模型类自行声明并实现自己的特征 recipe。

## 更新内容

### 1. 特征数值兜底

位置：`app/services/inference/offline/segmenters/clean.py`

在 `FeatureVectorizer` 中新增 finite 兜底逻辑：

- 对最终拼接出的 `[T, F]` 特征矩阵执行 `NaN/inf -> 0`；
- 在模型推理前，normalizer 处理之后再次执行 `NaN/inf -> 0`；
- 保持特征维度不变，只修正非法数值，避免异常值进入 torch 推理。

对应 review 点：R310 附近“特征无 NaN/inf 兜底，尺寸稳定但数值有洞”。

### 2. 删除 CLEAN 无权重规则降级

位置：`app/services/inference/offline/segmenters/clean.py`

删除了 `_RuleDecoder` 规则 fallback，并调整 `segment()` 行为：

- 未配置 `model_path` 时，CLEAN 离线模型直接 `ValueError` 硬失败；
- 不再静默输出规则分段；
- 本地无权重回环继续使用已有 `app.services.inference.offline.segmenters.mock.BrushRulesSegmenter`。

这样可以避免把 mock/规则结果误认为真实 CLEAN 离线模型结果。

对应 review 点：R389 附近“无权重还要做兜底吗？该硬失败；本地已有无权重 MOCK stage”。

### 3. 去掉基类内的 recipe 路由分支

位置：`app/services/inference/offline/segmenters/clean.py`

原先 `_CleanTorchSegmenter.transform_features()` 根据 `feature_method` 字符串做集中分发。本次改为：

- 基类 `transform_features()` 只保留虚函数默认行为；
- `CleanMSTCNBiLSTMSegmenter` 直接使用基础 v2 特征；
- `CleanASFormerSegmenter` 覆盖 `transform_features()`，追加 `business_priors`；
- `CleanBiGRUSegmenter` 覆盖 `transform_features()`，追加 `center_window + business_priors`。

三种模型仍然使用同一条离线数据流：

`FeatureStore -> FrameDetections -> clean.py preprocess -> ModelInput -> SegmentFact`

差异只在各模型把 `FrameDetections` 转成 `[T, F]` 输入特征时采用不同 recipe。

对应 review 点：R493 附近“为什么要设计路由，直接在各个 Segmenter 里实现该方法即可”。

### 4. 删除多余 TCN 层工厂类

位置：`app/services/inference/offline/segmenters/clean.py`

删除未使用的 `_DilatedResidualLayer`。当前 MS-TCN + BiLSTM 的实际实现已经在 `_make_mstcn_bilstm()` 内部定义 `DilatedResidualLayer`，旧类没有调用点，保留只会增加阅读成本。

对应 review 点：R761 附近“这个类是不是多余了，下边已经实现了工厂方法”。

### 5. 配置和测试同步

位置：

- `config/inference_config.yaml`
- `tests/test_offline_pipeline.py`

配置示例删除过期的 `fallback_to_rules` 参数，避免误导后续使用者。

单测同步调整：

- CLEAN preprocess 仍验证输出 `ModelInput`、113 维基础特征和 finite 数值；
- CLEAN 未配置 `model_path` 时验证硬失败；
- 确认硬失败时不写 `facts.jsonl` 和 `offline_inference_result.json`；
- MOCK stage 回环测试保持不变，继续覆盖无权重端到端链路。

## 验证结果

### 1. 语法检查

```powershell
.\.venv\Scripts\python.exe -m py_compile app\services\inference\offline\segmenters\clean.py
```

结果：通过。

### 2. 离线链路单测

```powershell
.\.venv\Scripts\python.exe -m pytest tests\test_offline_pipeline.py -q
```

结果：

```text
39 passed in 1.95s
```

### 3. 全量测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests\ -q
```

结果：

```text
335 passed in 42.69s
```

## 影响范围

- 不改变 FeatureStore / FactLedger / OfflineRunner 的主链路；
- 不引入训练代码；
- 不提交模型权重或数据集；
- CLEAN 离线模型现在必须配置真实 `model_path` 才能产出行为 timeline；
- 无权重测试入口统一使用 MOCK stage，职责更清晰。
