# 2026-07-19 CLEAN 离线:去掉 FeatureVectorizer 类抽象

## 背景

`app/services/inference/offline/segmenters/clean.py` 里特征工程被两层不必要的类抽象包着:

- `FeatureVectorizer` 是"全是 `@staticmethod` + 两个 config 字段"的类——无跨调用状态、无多态、无需守护的不变式,本质是"伪装成类的命名空间"。只负责基础 v2(113 维)。
- `_CleanTorchSegmenter` 又挂了一批同样无 `self` 的特征方法(`_add_business_priors` / `_add_centered_window_stats` + `_col` / `_near_score` / `_centered_mean` / `_with_features`),让本该只管"加载 + 推理 + 解码"的 segmenter 过载。

正确分层:**多态那一层(每个模型选自己的 recipe)留在 segmenter 的 `transform_features()` 接口;实现那一层(纯特征数学)是无状态函数**。据此 `FeatureVectorizer` 这个类没有存在必要。

## 更新内容

全部在 `clean.py` 内完成,保持"单策略自包含单文件",不新建文件。

### 1. 溶解 `FeatureVectorizer` 类 → 模块级函数

- `transform(...)` → `build_base_features(frames, fps, frame_width=640, frame_height=480)`(公开)
- `feature_names()` → `base_feature_names()`(公开,供训练仓对齐/调试)
- 其余方法降为模块私有函数,签名照搬:`_finite_matrix` / `_collect_object_arrays` / `_effective_fps` / `_frame_size` / `_bbox_to_center_area` / `_as_box5` / `_box_score` / `_missing_age` / `_impute_short_gaps` / `_select_hand_slots` / `_select_top1_slot` / `_build_feature_matrix`。
- 原 `self.frame_width/height` 改为函数入参。

### 2. 模型专属 recipe → 模块级函数(接口仍在 segmenter)

- `_add_business_priors` → `add_business_priors(mi)`(公开)
- `_add_centered_window_stats` → `add_centered_window_stats(mi, windows=(5,15))`(公开)
- `_with_features` / `_centered_mean` / `_col` / `_near_score` → 模块私有函数
- `transform_features()` 虚接口继续留在 `_CleanTorchSegmenter`(多态层不动)。

### 3. 收敛 `_CleanTorchSegmenter`

- `__init__` 删 `self.vectorizer = FeatureVectorizer(...)`,改存 `self.frame_width` / `self.frame_height`。
- `preprocess` 内联"跨源按 ts 融合"逻辑(不抽函数——单一 caller、无复用,YAGNI),末尾改调 `build_base_features(...)` + `self.transform_features(base)`。
- 从类里删除已下沉的 6 个特征方法;保留 `segment` / `_predict_with_model` / `_load_model` / `_labels_to_segments` / `_build_model`。
- 两个子类 `transform_features` 改调模块函数(`add_business_priors` / `add_centered_window_stats`)。

### 4. 文档字符串去"路由/分发"陈词

模块 docstring 去掉"可路由";`build_base_features` 说明改为"各模型在 `transform_features()` 内自行叠加";`_CleanTorchSegmenter` docstring 改述为"加载 + 推理 + 解码,`transform_features()` 为模型专属特征接口"。

## 验证结果

```bash
source .venv/bin/activate
python -m py_compile app/services/inference/offline/segmenters/clean.py   # 通过
pytest tests/test_offline_pipeline.py -q                                   # 39 passed
pytest tests/ -q                                                           # 335 passed
```

三个模型 `feature_dim`(113/121/249)、`feature_version` 字符串、无 `model_path` 硬失败、MOCK 端到端均不变。

## 影响范围

- 纯内部重构,零行为变化;`FeatureVectorizer` 无外部 importer,删类名对调用方零破坏。
- `ModelInput` 保留为 frozen dataclass;三个 `Clean*Segmenter` 类名 + `CleanSegmenter` 别名 + `feature_method` 类属性全部保留。
- `config/inference_config.yaml` 只引用 `Clean*Segmenter` 类路径,不受影响;测试无需改动。
