# CLEAN 离线模型特征 recipe 收敛：同一数据流下按模型生成不同输入

> **变更状态**：开发中（2026-07-15）  
> **承接**：基于 `feat/offline-infer` 已有的离线入口和 CLEAN stage 模型路由；本次只调整 CLEAN 离线模型内部的特征转换边界，不改变 `FeatureStore -> OfflineSegmenter -> SegmentFact -> FactLedger` 主链路。

## 背景

`F-NIAN/changhai-offline` 仓对 ActionMixed 数据集重新做了三模型最佳组合实验，结论是：

1. 三种模型消费的是同一条 CLEAN 离线数据流；
2. 数据流进入模型前，都先从 `FrameDetections` 序列转成基础 v2 特征；
3. 不同模型在基础 v2 上使用不同的特征增强 recipe，最终得到的模型输入维度不同。

也就是说，不是“三种模型数据来源不同”，而是“同一批检测序列在进入不同模型前，会按该模型的训练 recipe 转成不同维度的输入特征”。

| 模型 | 当前最佳特征组合 | 输入维度 |
|---|---|---:|
| `CleanMSTCNBiLSTMSegmenter` | `v2` | 113 |
| `CleanASFormerSegmenter` | `v2 + business_priors` | 121 |
| `CleanBiGRUSegmenter` | `v2 + center_window + business_priors` | 249 |

之前后端 `clean.py` 里只有一套全局特征转换逻辑，容易出现“模型权重按 A recipe 训练，后端推理却统一喂 B recipe 特征”的问题。尤其是 `F-NIAN/changhai-offline` 产出的 best checkpoint 内已经记录 `feature_names` 和 `feature_version`，后端必须按模型生成相同 recipe 的输入特征才能可靠加载。

本次改动把“基础数据流读取”和“模型输入特征 recipe”拆开：

```text
同一数据流:
FeatureStore.load_many(task_id, step_id, [clean_large, clean_small])
  -> Mapping[source, Sequence[FrameDetections]]
  -> 按 timestamp 合并成完整 CLEAN 检测序列

模型专属输入:
CleanMSTCNBiLSTMSegmenter  -> v2, 113维
CleanASFormerSegmenter     -> v2 + business_priors, 121维
CleanBiGRUSegmenter        -> v2 + center_window + business_priors, 249维
```

## 改动内容

### 1. 基础特征转换固定为 v2

`FeatureVectorizer` 现在只负责生成基础 `clean_bbox_v2_top1_impute` 特征，维度为 113。

基础 v2 特征包含：

- `hand` 使用 top-2 槽位，避免两只手被合并为一个不存在的中心点；
- 其它目标使用 top-1，不再做同类多框加权聚合；
- 每个目标包含 `present/conf/cx/cy/area/speed/missing_age/imputed`；
- 对关键目标对补充 `valid/dist/delta`；
- 最后补 `t_norm/t_sin/t_cos` 时间位置特征。

基础 v2 的输入来自后端在线检测落盘的 `FrameDetections`：

| 来源 | 内容 |
|---|---|
| `clean_large` | 大目标检测流，如 `hand`、`scope_control_body`、`scope_mid_section` 等 |
| `clean_small` | 小目标检测流，如 `short_brush`、`syringe`、`air_gun`、`scope_distal_end`、`brush_tip_out` 等 |

`preprocess()` 会按 timestamp 把多个 source 的检测框合并为一条完整时序序列。随后 `FeatureVectorizer` 将每一帧的 bbox 转成数值特征矩阵：

```text
features: [T, F]
T = 帧数
F = 特征维度，基础 v2 为 113
```

### 2. 特征增强变成模型级虚函数

`_CleanTorchSegmenter` 新增：

```python
transform_features(model_input: ModelInput) -> ModelInput
```

它是模型专属特征转换入口。默认使用基础 v2；具体模型通过 `feature_method` 选择自己的增强方式。

当前映射为：

| 模型类 | `feature_method` | 输出 `feature_version` |
|---|---|---|
| `CleanMSTCNBiLSTMSegmenter` | `v2` | `clean_bbox_v2_top1_impute` |
| `CleanASFormerSegmenter` | `business_priors` | `clean_bbox_v2_top1_impute+business_priors` |
| `CleanBiGRUSegmenter` | `window_stats+business_priors` | `clean_bbox_v2_top1_impute+center_window+business_priors` |

这样后续新增模型时，只需要在该模型类中声明/覆盖自己的特征 recipe，不需要再改全局特征转换。

### 3. 新增特征增强的含义

本次文档里的 `business_priors` 和 `center_window` 都不是新的数据源，也不是新的标签。它们是在基础 v2 特征之上，为离线时序模型追加的输入特征。

#### 3.1 business_priors 是什么

`business_priors` 可以理解为“业务弱先验特征”。它把一些清洗动作中相对稳定的业务关系，提前计算成数值列，作为模型输入的一部分。

它不是规则判定，也不会直接决定最终动作标签。最终输出仍然由模型根据完整特征序列推理得到。它的作用是降低小数据场景下模型从零学习业务关系的难度。

举例：

- 看到 `syringe` 靠近 `scope_distal_end`，并且比较稳定，模型更容易判断为 `flush`；
- 看到 `air_gun` 靠近 `scope_distal_end`，并且比较稳定，模型更容易判断为 `air_injection`；
- 看到 `short_brush`、`hand`、`scope_control_body` 同时出现并靠近，模型更容易判断为 `short_brush_cleaning`；
- 看到 `brush_tip_out` 或 `long_brush` 与内镜远端/中段有空间关系变化，模型更容易区分长刷插入或拔出。

当前一共追加 8 维：

| 特征名 | 主要使用的基础特征 | 含义 | 值域 |
|---|---|---|---|
| `prior_short_clean_near` | `hand_present`、`short_brush_present`、`short_brush_to_scope_control_body_dist` | 手和短刷存在，且短刷靠近控制部时更高 | 0-1 |
| `prior_short_clean_motion` | `short_brush_present`、`short_brush_speed`、`short_brush_to_scope_control_body_delta` | 短刷出现且有运动/距离变化时更高 | 0-1 |
| `prior_flush_stable` | `syringe_present`、`syringe_to_scope_distal_end_dist`、`syringe_speed` | 针筒靠近远端且相对稳定时更高 | 0-1 |
| `prior_air_stable` | `air_gun_present`、`air_gun_to_scope_distal_end_dist`、`air_gun_speed` | 气枪靠近远端且相对稳定时更高 | 0-1 |
| `prior_long_signal_near_scope` | `long_brush_present`、`brush_tip_out_present/imputed`、`*_to_scope_*_dist` | 长刷/刷头信号靠近内镜时更高 | 0-1 |
| `prior_long_towards_distal` | `brush_tip_out_to_scope_distal_end_delta` | 刷头距离远端变化方向偏“靠近”时更高 | 0-1 |
| `prior_long_away_distal` | `brush_tip_out_to_scope_distal_end_delta` | 刷头距离远端变化方向偏“远离”时更高 | 0-1 |
| `prior_hand_long_contact` | `hand_to_long_brush_dist`、`long_brush_present/brush_tip_out_present` | 手和长刷信号接近时更高 | 0-1 |

其中 `near` 类特征的直觉是：

```text
距离越近 -> 分数越高
距离越远 -> 分数越低
```

`stable` 类特征的直觉是：

```text
目标存在 + 靠近关键部位 + 速度较低 -> 更像正在推流/注气
```

`delta` 类特征的直觉是：

```text
目标到关键部位的距离变化方向，可以给插入/拔出提供弱提示
```

为什么需要它：

- 当前标注数据量小，纯模型很难稳定学到“哪个工具靠近哪个部位代表什么动作”；
- 小目标如 `syringe`、`air_gun`、`brush_tip_out` 容易漏检，弱先验可以让模型更关注关键空间关系；
- 它只是输入提示，仍保留模型对上下文的学习能力，不把最终判断写死成规则。

维度变化：

```text
基础 v2: [T, 113]
追加 business_priors: [T, 113 + 8] = [T, 121]
```

#### 3.2 center_window 是什么

`center_window` 是“中心窗口统计特征”。它利用离线模型可以看到未来帧的特点，对当前帧附近一小段时间内的关键特征做均值统计。

在线实时模型只能看过去和当前帧，但离线模型是在任务结束后批处理，所以可以看：

```text
当前帧之前的帧 + 当前帧 + 当前帧之后的帧
```

因此，`center_window` 对每一帧都计算一个以当前帧为中心的局部均值。当前使用两个窗口：

```text
w = 5
w = 15
```

直观例子：

```text
某一帧 syringe 没检出，但前后几帧都稳定检出了 syringe。

基础 v2:
当前帧 syringe_present = 0

center_window:
syringe_present_center_mean_w5 可能接近 0.8
syringe_present_center_mean_w15 可能接近 0.6
```

这样模型能知道：虽然当前帧检测器漏了一下，但局部时间窗口里这个目标大概率一直存在。

参与中心窗口统计的列不是全部 113 维，而是动作相关、对遮挡和时序更敏感的列。当前选择名称后缀为：

```text
_present
_conf
_speed
_dist
_delta
_missing_age
_imputed
```

含义分别是：

| 后缀 | 表示什么 | 为什么适合做窗口统计 |
|---|---|---|
| `_present` | 当前帧真实检测是否出现 | 平滑短时漏检 |
| `_conf` | 检测置信度 | 观察目标是否持续可信 |
| `_speed` | 目标运动强度 | 捕捉刷洗/移动趋势 |
| `_dist` | 两个目标之间距离 | 捕捉工具和内镜部位的接近关系 |
| `_delta` | 距离变化方向/幅度 | 捕捉插入、拔出、靠近、远离 |
| `_missing_age` | 连续缺失时长 | 区分短遮挡和长期不存在 |
| `_imputed` | 是否由短遮挡补全得到 | 告诉模型该值是补全值，不是真实检测 |

计算方式可以理解为：

```text
对每个被选中的基础列 x:

当前帧 i 的 w=5 特征 =
    mean(x[i-2], x[i-1], x[i], x[i+1], x[i+2])

当前帧 i 的 w=15 特征 =
    mean(x[i-7] ... x[i] ... x[i+7])
```

序列开头和结尾不足窗口长度时，只使用实际存在的帧。

当前基础 v2 中有 64 个列会参与窗口统计。每个列追加两个窗口均值：

```text
64 个基础列 * 2 个窗口 = 128 个新增特征
```

维度变化：

```text
基础 v2: [T, 113]
追加 center_window: [T, 113 + 128] = [T, 241]
再追加 business_priors: [T, 241 + 8] = [T, 249]
```

为什么需要它：

- 医院清洗视频里经常有手部遮挡，小目标会短时消失；
- 离线模型不受实时约束，可以使用未来帧帮助当前帧判断；
- 对边界判断来说，局部窗口里的持续出现/持续消失比单帧检测更稳定；
- BiGRU 本身能读前后文，显式加入窗口统计后，在当前实验里效果最好。

需要注意：

- 它不会改变原始检测数据，只是在模型输入矩阵上追加统计列；
- 它不是平滑最终预测结果，而是在模型推理前给模型更多局部上下文特征。

### 4. 三种模型的具体特征转换

#### 4.1 MS-TCN + BiLSTM：基础 v2，113 维

对应类：

```python
CleanMSTCNBiLSTMSegmenter
```

对应 recipe：

```text
feature_method = v2
feature_version = clean_bbox_v2_top1_impute
feature_dim = 113
```

转换流程：

```text
FrameDetections
  -> timestamp 对齐
  -> hand top-2
  -> 其它目标 top-1
  -> 遮挡短缺失补全
  -> 目标自身特征
  -> 目标对关系特征
  -> 时间位置特征
  -> [T, 113]
```

主要特征内容：

- `hand_count`
- `hand_top1_*`、`hand_top2_*`
- `short_brush_*`、`syringe_*`、`air_gun_*`
- `scope_control_body_*`、`scope_mid_section_*`、`scope_distal_end_*`
- `brush_tip_out_*`
- `hand_to_short_brush_valid/dist/delta`
- `brush_tip_out_to_scope_distal_end_valid/dist/delta`
- `short_brush_to_scope_control_body_valid/dist/delta`
- `air_gun_to_scope_distal_end_valid/dist/delta`
- `syringe_to_scope_distal_end_valid/dist/delta`
- `t_norm/t_sin/t_cos`

这里 MS-TCN + BiLSTM 直接吃基础 v2。它依靠 BiLSTM 读取前后文，TCN stage 再扩大时间感受野并细化逐帧分类，因此没有额外追加业务先验或中心窗口统计。

#### 4.2 ASFormer：v2 + business_priors，121 维

对应类：

```python
CleanASFormerSegmenter
```

对应 recipe：

```text
feature_method = business_priors
feature_version = clean_bbox_v2_top1_impute+business_priors
feature_dim = 121
```

转换流程：

```text
FrameDetections
  -> 基础 v2 [T, 113]
  -> 追加 8 个业务先验分数
  -> [T, 121]
```

追加的业务先验不是规则输出，而是作为模型输入的弱提示。当前包含：

| 先验特征 | 含义 |
|---|---|
| `prior_short_clean_near` | 手、短刷、内镜控制部/远端邻近时，提示短刷清洗可能性 |
| `prior_short_clean_motion` | 短刷运动或短刷到控制部距离变化，提示刷洗动作 |
| `prior_flush_stable` | 针筒靠近远端且相对稳定，提示推流 |
| `prior_air_stable` | 气枪靠近远端且相对稳定，提示注气 |
| `prior_long_signal_near_scope` | 长刷/刷头信号靠近内镜，提示长刷插拔相关动作 |
| `prior_long_towards_distal` | 刷头相对远端距离变化方向，提示插入趋势 |
| `prior_long_away_distal` | 刷头相对远端距离变化方向，提示拔出趋势 |
| `prior_hand_long_contact` | 手与长刷接近，提示人工操作长刷 |

ASFormer 的核心是用 attention 建立较长范围的时序依赖。当前小数据场景下，直接让 attention 从纯 bbox 特征中学习全部业务关系不稳定，所以给它追加 8 维业务先验作为辅助输入。

#### 4.3 BiGRU：v2 + center_window + business_priors，249 维

对应类：

```python
CleanBiGRUSegmenter
```

对应 recipe：

```text
feature_method = window_stats+business_priors
feature_version = clean_bbox_v2_top1_impute+center_window+business_priors
feature_dim = 249
```

转换流程：

```text
FrameDetections
  -> 基础 v2 [T, 113]
  -> 对动作相关列追加中心窗口统计 [T, +128]
  -> 追加 8 个业务先验分数
  -> [T, 249]
```

中心窗口统计使用离线模型的优势：可以看当前帧之后的帧。当前窗口为：

```text
w = 5
w = 15
```

参与窗口统计的列包括名称后缀为以下类型的特征：

```text
_present
_conf
_speed
_dist
_delta
_missing_age
_imputed
```

每个被选中的基础列都会追加：

```text
<feature_name>_center_mean_w5
<feature_name>_center_mean_w15
```

BiGRU 本身是双向循环模型，能利用过去和未来上下文；中心窗口统计进一步把短时间邻域内的检测稳定性、遮挡、距离变化显式暴露给模型。当前实验里这一组合效果最好。

### 5. 权重加载增加输入一致性校验

加载 `.pt` 时会校验：

- checkpoint 内的 `feature_names` 是否等于当前模型实际生成的 `feature_names`；
- checkpoint 内的 `feature_version` 是否等于当前模型实际生成的 `feature_version`。

如果不一致，直接 fail-fast，避免静默产出错误 timeline。

### 6. 保留 fallback 行为

未配置 `model_path` 时，仍可使用规则 fallback 做本地回环测试。fallback 已兼容新的 v2 特征名，优先读 `*_present` / `*_candidate_count`，历史 68 维输入才回退到 `*_count`。

## 和离线链路的关系

主链路不变：

```text
FeatureStore.load_many(task_id, step_id, subscribes)
  -> OfflineSegmenter.preprocess(streams)
  -> Clean*.transform_features(model_input)
  -> Clean*.segment(model_input)
  -> SegmentFact
  -> FactLedger.replace_segments(...)
```

本次变更只发生在 CLEAN 模型策略内部，不改变 Runner、FactLedger、YAML 路由和 CLI 手动启动方式。

准确地说，三种模型不是三条独立数据链路。它们共享：

- 同一个 `FeatureStore.load_many(...)` 入口；
- 同一批 `clean_large` / `clean_small` 检测结果；
- 同一个 `OfflineRunner`；
- 同一种 `SegmentFact` 输出契约；
- 同一个 `FactLedger.replace_segments(...)` 写入方式。

差异只发生在：

```text
FrameDetections -> ModelInput(features)
```

这一层。也就是同一条检测序列进入不同模型前，会被各自的 `transform_features()` 转成对应 checkpoint 所需的输入维度和列顺序。

## 验证

离线链路单测：

```bash
python -m pytest tests/test_offline_pipeline.py -q
```

结果：

```text
39 passed
```

额外做了 `F-NIAN/changhai-offline` best 权重的加载冒烟测试：

| 模型 | 输入维度 | 权重加载/推理 |
|---|---:|---|
| `CleanMSTCNBiLSTMSegmenter` | 113 | 通过 |
| `CleanASFormerSegmenter` | 121 | 通过 |
| `CleanBiGRUSegmenter` | 249 | 通过 |

并确认后端生成的 `feature_names` 与以下 best checkpoint 完全一致：

- `best_ms_tcn_offline_segmenter.pt`
- `best_asformer_offline_segmenter.pt`
- `best_bigru_offline_segmenter.pt`

## 注意事项

- 三种模型共享同一条 CLEAN 检测数据流，但不能跳过模型专属 `transform_features()` 后直接共用同一个 `[T, F]` 特征矩阵；切换 YAML `offline.class` 的同时，应配套使用该模型对应的 checkpoint。
- 当前只是模型输入与权重对齐，不代表已经实现自动调度、离线 Judge、告警或结果入库。
