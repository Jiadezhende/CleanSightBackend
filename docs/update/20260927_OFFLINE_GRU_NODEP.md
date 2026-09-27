# CLEAN 离线接入因果滑窗 GRU（nodep-226d 特征），设为默认离线模型

> **变更状态**：生效中（2026-09-27）
> **知识库**：待沉淀

## 概述

新增离线策略 [`CleanNodepGRUSegmenter`](../../app/services/inference/offline/impl/clean.py)，接入训练框架交付的
`ama-v3-concat23-nodep-226d + GRU(w=16)` 权重。它输出 6 类，特征与训练框架逐位对齐。CLEAN（step `2`）`offline.class` 改为该类，
部署物料 `clean-offline-gru-nodep.pt` 由 `scripts/pack_clean_nodep_gru.py` 从训练交付打成自包含单文件（内嵌 meta）。本次只负责接入，不评估模型效果。

## 变更背景

- **现状**：离线 CLEAN 三个模型（MS-TCN / ASFormer / BiGRU）都是整段双向前向，吃后端移植的 v2 特征及其 recipe；
  标签表 `ACTION_LABELS` 写死在模块级。
- **新模型与现有管线的差异**（据模型卡、`.meta.json`、checkpoint 实测）：

| 项 | 现有整段模型 | 新模型 |
|----|------------|-------|
| 特征 | v2（113）+ recipe | v2 ⊕ v3 拼接（226），v3 = scope 器械轴坐标系；废弃类 scope_distal_end / short_brush / long_brush 在读入层丢弃 |
| 推理 | 整段一次前向 `[1,T,F] → [1,C,T]` | 每帧取以它为末帧的 16 帧窗口 `[T,16,226] → [T,C]`，单向 GRU |
| 类别 | idle / long_brush_insert / … / air_injection | idle / water_injection / flush / long_brush_insert / long_brush_withdraw / short_brush_cleaning（按权重原样） |
| checkpoint | `state_dict` + `feature_names` + normalizer | `{checkpoint_kind: training_state, model_state, optimizer_state, …}`，无 normalizer / 列名 / 类名；网络与特征契约在旁挂的 `.meta.json` |
| 入模帧率 | 按 ts 估计 | 训练以固定 fps 切帧，窗口与补缺上限按帧计数 |

- **参考实现**：训练侧交付的 `clean_bbox_v2.py` / `clean_bbox_v3.py` / `nodep_concat.py`（仓库 `ref/`，gitignore）。
  框架 v2 与后端既有 v2 移植逐函数一致，只有读入层不同（框架读 YOLO txt 的 `cx cy w h [conf]`，后端读 `FrameDetection` 的 xyxy）。

## 方案详情

### 全景

```text
read_detections(task, step)                     ← 15fps（raw_fps / inference_decimation）
  │ preprocess
  ├─ resample_by_ts(frames, model_input_fps)    ← 只挑真实帧，ts 与 detections.jsonl 位级相等
  ├─ _collect_object_arrays(confidence_override) → 清空 3 个废弃类
  ├─ v2 = _build_feature_matrix(113) ⊕ v3 = _build_v3_matrix(113)   → ModelInput [T,226]
  │ segment
  ├─ 首次：torch.load 自包含物料 → 校验内嵌 meta 的特征契约 / 类别数 / pipeline → 按 meta.model 建 GRU → strict 载 model_state
  ├─ _causal_windows(x, meta.window)  [T,16,226]（开头不足一窗用首帧重复补齐）→ 分批前向 → softmax [T,6]
  └─ 逐帧 argmax → TemporalSegment（沿用基类解码）+ LabelProbs(labels = 6 类)
```

| 部件 | 落在哪 | 说明 |
|------|--------|------|
| 降采样 | `app/services/inference/resample.py` | 复用在线同一算法（前序提交已上提为共享函数） |
| 置信度口径 | `_collect_object_arrays(confidence_override=)` | 非 None 时所有框置信度改用该值；旧调用不传，行为不变 |
| v3 特征 | `_forward_fill` / `_scope_frame` / `_v3_slot_channels` / `_build_v3_matrix` | 逐函数对齐框架 `clean_bbox_v3.py`，复用后端 v2 的选框 / 插值 |
| nodep 拼接 | `build_nodep_concat_features` | 列名加 `v2.` / `v3.` 前缀区分两半 |
| 标签表 | `_CleanTorchSegmenter.labels` 类属性 | 原模块级 `ACTION_LABELS` 的 4 处引用改读 `self.labels`；旧三类默认值不变 |
| 模型 / 加载 / 推理 | `_make_window_gru` / `_check_window_gru_meta` / `CleanNodepGRUSegmenter` | 网络按 state_dict 键 `rnn.*`（`nn.GRU`）+ `head.*`（`nn.Linear`）重建 |
| 物料打包 | `pack_window_gru_checkpoint` + `scripts/pack_clean_nodep_gru.py` | 训练交付 `x.pt` + `x.pt.meta.json` → `{model_state, meta}`；丢 optimizer 等训练态（4.0MB → 1.35MB） |
| 配置 | `config/inference_config.yaml` step `2` offline | class 换新类；新增 `model_input_fps: 7.5`、`confidence_override: 1.0` |

### 关键约定

- **部署物料自包含**：`.pt` 内嵌训练框架 meta，运行时不读旁挂文件。窗口长度与网络超参只从内嵌 meta 读，不进 YAML。
- **校验分两处**：打包时校验 sha256 绑定（meta ↔ 原始 checkpoint）+ 契约；加载时再校验契约（特征版本 / 维度、`model.type/input_dim/num_classes`、
  pipeline = `sliding_window_temporal`），不符均 `ValueError`。未打包的训练原始 checkpoint（无内嵌 meta）加载即 `ValueError`。
- **`model_input_fps` 必填**（无默认，缺即 `TypeError`）；`confidence_override` 缺省 `None`（用真实置信度），YAML 显式写 1.0。
- **`LabelProbs` 只覆盖降采样后的帧**（7.5fps），lab 概率读口按 ts 逐点换算媒体刻度，不要求与检测帧一一对应。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 特征逐位对齐 | 合成 72 帧序列（多 hand 候选、同类多框、≤6 帧与长缺口、scope 轴三种回退、废弃类与未知类，分两流）经框架参考实现生成 `tests/fixtures/clean_nodep_golden/expected.npz`；后端输出在 `conf_default`（5 列标注 + 1.0）与 `conf_real`（6 列）两种口径下均 `atol=1e-5` 相等 |
| `tests/test_offline_clean_nodep_gru.py` | 15 passed：对齐、废弃类块、空输入、因果窗口、构造参数校验、降采样保留真实 ts、小权重端到端（打包后删旁挂 meta 再加载；6 类概率 / ts 对齐 / 行和为 1）、窗口因果性、打包拒三类不符 meta、打包丢训练态、加载拒类别数不符、加载拒未打包 checkpoint |
| 真实权重 + 真实 YAML（临时目录放物料，`CLEANSIGHT_MODEL_PATH` 指过去） | `StageFactory` 建出 `CleanNodepGRUSegmenter`；sha256 校验通过、`strict=True` 加载成功、window=16；输出 `[T,6]` 行和为 1；4500 帧（10 分钟 @7.5fps）前向 0.33s（CPU） |
| 真实权重打包 | `pack_clean_nodep_gru.py` 打包通过；单文件加载 window=16、输出 `[T,6]` 行和为 1 |
| 全量 `pytest tests/` | 965 passed, 8 skipped |
| 集成测试 | 未跑（需真实环境，交由人工） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **部署须放新物料**：`python scripts/pack_clean_nodep_gru.py gru_nodep226d_w16_seed42_best.pt app/data/clean-offline-gru-nodep.pt`（源 `.pt` 同目录须有其 `.meta.json`） | 缺失则 CLEAN 离线作业失败（不影响在线） | 随模型物料分发（分发打包后的单文件） |
| `model_input_fps=7.5`、`confidence_override=1.0` 未经训练侧确认 | 配错不报错，特征静默偏离训练分布 | 训练侧确认后只改 YAML |
| 开头不足一窗的帧用首帧重复补齐，未确认与训练评估一致 | 仅影响每个 step 开头约 2 秒 | 训练侧确认后按需改 `_causal_windows` |
| GRU 结构按 state_dict 键推断（GRU 末帧输出直接进 Linear），框架 `build_model` 源码未见 | 若框架在两者之间有激活等无参层，输出会偏 | 训练侧确认或补交 `build_model` 源码 |
| 特征按整段计算（`t_norm`、v3 中位数回退、v2 插值用到右侧帧），不是因果的 | 离线无碍（前提：训练视频与后端 step 粒度一致）；模型卡「因果、流式可用」不成立，不能原样搬到在线 | 待训练侧确认视频粒度；上在线前需重新设计特征 |
| `resample_by_ts` 在输入帧率 ≈ 目标帧率时，浮点 ts 会让部分帧被跳过（合成 72 帧 @7.5fps → 7.5 只保留 61 帧） | 默认 15fps → 7.5fps 不受影响；把 `inference_decimation` 调到检测率 = 7.5 时会丢约 15% 帧，在线同样如此 | 需要时给网格比较加半帧容差（在线离线一并改） |
