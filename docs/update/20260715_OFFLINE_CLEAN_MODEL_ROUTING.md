# 离线 CLEAN 模型路由与本地权重接入

## 背景

本次更新承接 `20260715_OFFLINE_CONVERGE.md` 的收敛方向，继续保持离线链路只走一条主路径：

```text
FeatureStore.load_many(task_id, step_id, subscribes)
  -> OfflineSegmenter.preprocess(...)
  -> OfflineSegmenter.segment(...)
  -> SegmentFact
  -> FactLedger.replace_segments(...)
```

本次重点是把 CLEAN 阶段真实时序模型接入口收敛到 YAML 路由体系，并验证本地
`offline-model` 仓训练出的三份 `.pt` 权重可以被后端严格加载和完整推理。

## 主要变更

### 1. OfflineSegmenter 只保留通用接口

`app/services/inference/offline/segmenter.py` 中 `preprocess()` 改为抽象方法。
框架层不再默认透传，也不做特征工程；具体如何从 `FrameDetections` 提取模型输入，
由每个策略类自行实现。

这样后续不同 stage 可以各自维护自己的输入转换逻辑，例如 CLEAN 阶段使用 bbox 时序特征，
其它阶段可以使用不同检测源或不同模型输入。

### 2. MOCK 默认路由保留轻量兜底

`app/services/inference/offline/segmenters/mock.py` 保留 `BrushRulesSegmenter`：

- 不依赖 torch 或权重文件；
- 只根据订阅 source 是否存在检测框生成 active 片段；
- 用于 MOCK stage 的端到端 smoke test 和非法配置兜底。

`config/inference_config.yaml` 中 MOCK stage 默认启用该策略；CLEAN stage 默认仍为
`offline: {}`，避免生产环境在没有权重时误启用真实模型。

### 3. CLEAN 三种模型集中到单文件策略

`app/services/inference/offline/segmenters/clean.py` 内聚 CLEAN 离线推理所需内容：

- `FeatureVectorizer`：把 `FrameDetections` 序列转换为 CLEAN 专属 68 维时序特征；
- `CleanMSTCNBiLSTMSegmenter`：MS-TCN + BiLSTM；
- `CleanASFormerSegmenter`：ASFormer 风格时序 attention；
- `CleanBiGRUSegmenter`：BiGRU；
- `CleanSegmenter`：兼容别名，默认指向 `CleanMSTCNBiLSTMSegmenter`。

后端不包含训练流程。训练、导出权重仍在独立 `offline-model` 仓完成；后端只负责加载
已训练 checkpoint 并执行离线推理。

### 4. 与本地 offline-model checkpoint 严格对齐

本次已把后端三种模型结构对齐到本地 `offline-model/output_actionmixed_full/models/`
下的三份 checkpoint：

```text
ms_tcn_offline_segmenter.pt   -> CleanMSTCNBiLSTMSegmenter
asformer_offline_segmenter.pt -> CleanASFormerSegmenter
bigru_offline_segmenter.pt    -> CleanBiGRUSegmenter
```

加载权重时使用 `strict=True`。如果后端模型结构和训练仓保存的参数名或 shape 不一致，
会直接失败，避免 `strict=False` 静默跳过权重导致“看起来跑通但实际没加载模型”。

### 5. 兼容 PyTorch 2.6 checkpoint 加载

PyTorch 2.6 起 `torch.load()` 默认 `weights_only=True`。当前 offline-model checkpoint
除了 `state_dict`，还包含：

```text
normalizer_mean
normalizer_std
feature_names
class_names
model_name
```

其中 normalizer 使用 numpy 对象保存，默认安全加载会拒绝反序列化。因此后端加载本地可信
checkpoint 时显式使用：

```python
torch.load(path, map_location="cpu", weights_only=False)
```

并保留旧 PyTorch 的 `TypeError` fallback。

加载后还会校验 checkpoint 内的 `feature_names` 与后端
`FeatureVectorizer.feature_names()` 完全一致，防止训练仓和后端特征列错位。

## 特征输入格式

CLEAN 模型不直接吃原视频帧，也不直接吃 `features.jsonl` 文本。实际输入转换为：

```text
FrameDetections 序列
  -> FeatureVectorizer
  -> features[T, 68]
  -> checkpoint normalizer 标准化
  -> torch tensor[1, T, 68]
  -> logits[1, 6, T]
  -> SegmentFact
```

68 维特征主要包括：

- `hand` top-2 槽位：两只手独立保留 `present/cx/cy/area/speed`，避免被加权合并；
- 其它目标：`short_brush/long_brush/syringe/air_gun/scope_*` 等对象的
  `count/cx/cy/area/speed`；
- 关键对象对距离：如 `hand_to_short_brush`、`syringe_to_scope_distal_end`；
- 时间位置编码：`t_norm/t_sin/t_cos`。

bbox 归一化兼容像素 `xyxy` 和 0-1 `xyxy`。`speed` 使用同一对象上一次出现帧到当前帧的
真实 timestamp 差值 `dt` 计算，`dt` 异常时才回退到配置 fps。

## YAML 启用方式

CLEAN 默认不启用离线真实模型：

```yaml
offline: {}
```

需要启用时，在对应 stage 配置 `offline.class` 和 `params.model_path`。例如 MS-TCN + BiLSTM：

```yaml
offline:
  name: clean_offline
  subscribes: [clean_large, clean_small]
  class: app.services.inference.offline.segmenters.clean.CleanMSTCNBiLSTMSegmenter
  params:
    model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/clean-offline-mstcn-bilstm.pt
    fallback_to_rules: false
```

切换模型时只需要换 `class` 和权重路径：

```text
app.services.inference.offline.segmenters.clean.CleanASFormerSegmenter
app.services.inference.offline.segmenters.clean.CleanBiGRUSegmenter
```

## 验证结果

### 1. 本地权重严格加载

使用最小 68 维输入验证三份 checkpoint 均可 strict 加载并 forward：

```text
ms_tcn loaded ms_tcn_offline_segmenter.pt facts 0 frames 5 feature_dim 68
asformer loaded asformer_offline_segmenter.pt facts 3 frames 5 feature_dim 68
bigru loaded bigru_offline_segmenter.pt facts 1 frames 5 feature_dim 68
```

### 2. offline-model 真实 `.npz` 特征直推

使用 `offline-model/output_actionmixed_full/feature_store/task_21640639_step_1.npz`：

```text
frames: 296
feature_dim: 68
fps: 7.5

ms_tcn:
  segments: 6
  frame_predictions: 296
  label_hist: long_brush_insert=259, flush=21, idle=16

asformer:
  segments: 24
  frame_predictions: 296
  label_hist: idle=19, long_brush_insert=118, air_injection=50, flush=109

bigru:
  segments: 1
  frame_predictions: 296
  label_hist: long_brush_insert=296
```

### 3. 后端完整链路回环

构造 75 帧后端格式 `features.jsonl`，分别通过三种模型走完整链路：

```text
features.jsonl
  -> FeatureStore.load_many
  -> OfflineRunner
  -> Clean*Segmenter
  -> SegmentFact
  -> FactLedger(facts.jsonl)
  -> offline_inference_result.json
```

结果：

```text
ms_tcn:
  result: completed
  producer: ms_tcn_offline
  segment_count: 4
  debug_frames: 75
  feature_dim: 68

asformer:
  result: completed
  producer: asformer_offline
  segment_count: 7
  debug_frames: 75
  feature_dim: 68

bigru:
  result: completed
  producer: bigru_offline
  segment_count: 1
  debug_frames: 75
  feature_dim: 68
```

### 4. pytest

离线相关测试：

```bash
python -m pytest tests/test_offline_pipeline.py tests/test_offline_reservation.py -q
```

结果：

```text
42 passed in 2.09s
```

全量回归：

```bash
python -m pytest tests/ -q
```

结果：

```text
334 passed in 39.78s
```

## 边界与后续

- 三份 `.pt` 权重不纳入后端仓库；权重训练和管理仍由 offline-model / 模型平台负责。
- 当前权重是 1 epoch baseline，结果主要验证工程链路和数据格式闭环，不代表最终精度。
- 自动任务结束后触发离线 Runner、离线 Judge、合规判断、告警和业务入库仍未实现。
- 后续真实模型更新时，需要保证 checkpoint 的 `feature_names` 与后端特征列完全一致。
