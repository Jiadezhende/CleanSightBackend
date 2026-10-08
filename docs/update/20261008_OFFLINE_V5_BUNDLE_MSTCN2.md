# CLEAN 离线接入整段 MS-TCN2（ama-v5-bundle-94d 特征），设为默认离线模型

> **变更状态**：生效中（2026-10-08）
> **知识库**：待沉淀

## 概述

新增离线策略 [`CleanV5BundleMSTCN2Segmenter`](../../app/services/inference/offline/impl/clean.py)，接入训练侧交付的
`ama-v5-bundle-mstcn2-v41`（94 维特征 + MS-TCN2，6 类）。CLEAN（step `2`）`offline.class` 改为该类，权重
`clean-offline-mstcn2-v5bundle.pt` 单文件加载。特征、前向、后处理与训练侧参考实现逐位对齐；本次不评估模型效果。

## 变更背景

- **现状**：默认离线模型是因果滑窗 GRU（nodep-226d，7.5 fps）。训练侧新一版 v41 在 insert / withdraw 段级 F1@0.25
  上主指标 64.4（pp20），是目前最好的配方。
- **新模型与 nodep GRU 的差异**（据模型卡、`pin.yaml`、`feature_columns.json`、checkpoint 实测）：

| 项 | nodep GRU | v5-bundle MS-TCN2 |
|----|-----------|-------------------|
| 入模帧率 | 7.5 fps | 15 fps（= 检测帧率，降采样等于原样放行） |
| 检测预筛 | 无 | 每帧每类按面积留前 K 个框（hand 2、其余 1），对齐训练标注 auto-annotate 的 top-K |
| 特征 | v2 113 ⊕ v3 113（control→distal 轴，前向填充） | abs 65（v2 去废弃类 / 死 pair）⊕ scope 18（ctrl→mid 轴，锚点双向插值 + 1 s 中值平滑，仅几何通道）⊕ axis 2 ⊕ pair 9（hand/ctrl/mid） |
| 网络 | 单向 GRU，16 帧窗口 | MS-TCN2：2 stage × 5 层，hidden 32，双向卷积，整段一次前向；z-score 统计在 buffer 内 |
| 后处理 | argmax → 丢弃短于 `min_duration_s` 的段 | argmax → 短于 20 帧的段并入相邻较长段（训练侧唯一认可的后处理，不含顺序先验） |
| checkpoint | `{model_state, optimizer_state, …}` | 同格式，`model_state` 含 `norm_mean / norm_std` |

- **参考实现**：训练仓 `tools/infer_v5_bundle.py`、`tools/features_ama/clean_bbox_v5.py`、
  `tools/extract_ama_features.py::build_candidate_columns`、`tools/eval_protocol.py::merge_short_segments`、
  `framework/.../models/mstcn2.py`（多数未提交到训练仓 Git）。其中 `features_ama/clean_bbox_v2.py / v3.py` 与后端已移植的
  v2 / v3（nodep 接入时对齐过）逐字节相同，后端直接复用。

## 方案详情

### 全景

```text
read_detections(task, step)                         ← 15fps
  │ preprocess
  ├─ resample_by_ts(frames, 15.0, strict=True)       ← 检测帧率低于 15 即 ValueError
  ├─ _collect_top_area_arrays(confidence_override=1.0)  每帧每类面积 top-K，丢废弃类；打包成每类 ≤K 个 [T,5]
  ├─ abs   = _build_feature_matrix(113) 去废弃类列 / 死 pair 列        → 65
  ├─ 槽位选择 + 短缺口插值（复用 v2）→ _v5_scope_frame（ctrl→mid 轴，双向插值 + 中值平滑）
  ├─ scope = _v3_slot_channels(…, origin) 取 along/across/log_area_ratio，去 3 个轴退化列 → 18
  ├─ axis  = [axis_observed, axis_age]                                   → 2
  └─ pair  = _pair_channels × 3（hand↔ctrl、hand↔mid、ctrl↔mid）          → 9   ModelInput [T,94]
  │ segment
  ├─ 首次：按固定结构（hidden 32 / 2 stage / 5 层）建 MS-TCN2 → strict 载 model_state
  ├─ 整段前向 → softmax [T,6]（落 label_probs，后处理前）
  └─ _frame_labels：argmax → merge_short_runs(min_segment_frames=20) → TemporalSegment
```

| 部件 | 落在哪 | 说明 |
|------|--------|------|
| 检测预筛 | `_collect_top_area_arrays` | 按面积降序取 top-K，与 auto-annotate 同口径；打包表示与「每框一个稀疏数组」对 `_select_*` 等价，内存 O(T·K) |
| 器械轴 | `_interp_fill` / `_median_smooth` / `_v5_scope_frame` | 逐函数对齐 `clean_bbox_v5.scope_frame_offline`（smooth=True） |
| 特征拼装 | `build_ama_v5_bundle_features` | 列名、列序与 `feature_columns.json` 一致（`abs/` `scope/` `pair/` 前缀） |
| pair 抽取 | `_pair_channels` | 从 `_build_v3_matrix` 的 pair 循环原样抽出，v3 与 v5 共用 |
| 后处理 | `merge_short_runs` + `_CleanTorchSegmenter._frame_labels` 钩子 | 基类默认 argmax；段置信度改为取所判标签的概率（argmax 时与原 `max` 相同，旧模型行为不变） |
| 模型 / 加载 | `_make_mstcn2` / `CleanV5BundleMSTCN2Segmenter` | 网络按 state_dict 键重建，结构写死；单文件加载，不读 `.meta.json` |
| 配置 | `config/inference_config.yaml` step `2` offline | `model_input_fps: 15.0`、`confidence_override: 1.0`、`min_segment_frames: 20` |

### 关键约定

- **单文件加载**：只读 `.pt`。网络结构与 nodep GRU 一样写死在策略类，结构不符由 `strict=True` 报错；特征版本无运行时校验。
- **`min_duration_s` 固定为 0**：本模型只做「短段并入」一种后处理；`min_segment_frames: 0` 即原始 argmax 输出。
  需要刷洗时长类判定时用 `label_probs`（后处理前的原始概率）。
- **权重选用 `fold0-seed42.pt`**：交付包只有 5 折 × 4 seed 的验证权重，每个只见过 4/5 的视频；挑参考实现核对过的这一个。

## 变更效果

**自测结果**

| 项 | 结果 |
|----|------|
| 特征 / 前向 / 后处理与参考实现对齐（开发期一次性验证，未入库） | 合成 300 帧 ×7 组（每类 1–3 框含废弃类、30% 缺口、轴长 < 0.03 段、缺 ctrl、缺 mid、全空、单帧；参考侧喂面积 top-K 预筛后的框文件）：94 列 **max\|d\| = 0**。真实视频 3 段（6d8c7af2 / 52d2541c / 4894e7ba，各用未见过它的折权重）：特征 max\|d\| ≤ 2.1e-5（xyxy↔xywh 浮点换算），概率 max\|d\| ≤ 6.6e-7，argmax 与后处理后逐帧标签**完全一致** |
| 真实权重 + 真实 YAML | `StageFactory` 建出 `CleanV5BundleMSTCN2Segmenter`（fps 15 / min_segment_frames 20 / conf 1.0）；fold0-seed42 strict 加载成功 |
| 耗时（CPU，合成 10 分钟 step = 9000 帧，每帧 ~8 框） | 特征 0.9 s，前向 + 解码 1.3 s（训练侧参考实现约 6 min / 3000 帧） |
| 全量 `pytest tests/` | 983 passed（不新增逐模型用例） |
| 集成测试 | 未跑（需真实环境，交由人工） |

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| **部署须放新物料**：`app/data/clean-offline-mstcn2-v5bundle.pt`（由 `ref/ama-v5-bundle-mstcn2-v41/weights/fold0-seed42.pt` 改名） | 缺失则 CLEAN 离线作业失败（不影响在线） | 随模型物料分发 |
| 上线权重只见过 4/5 训练视频，模型卡指标不对应任何单个权重 | 实际效果可能略低于卡上 64.4 | 请训练侧按同配方在全部 43 个视频上重训一个最终权重，到货后只换文件 |
| 后端检测权重 `clean-large-best.pt` / `clean-small-best.pt` 未核实就是训练用的 `yolo11s-g1-v1` / `g2-v1`（类表一致，训练机未存权重，无法比对 sha） | 检测器不同 → 特征分布偏移，不报错 | 请训练侧给两份权重的 sha256 核对 |
| NMS IoU：后端 0.45，训练标注用 ultralytics 默认（0.7） | 预筛按面积 top-K 后影响小，未量化 | 观察；必要时对齐 |
| 特征含视频内相对时间（`t_norm / t_sin / t_cos`），训练视频是剪辑片段，后端是整段 step | 剪辑范围差异大时可能偏移；也是隐式顺序先验 | 模型卡建议用违规模拟视频检查，必要时请训练侧去掉 3 列重训 |
| nodep GRU 未加检测 top-K 预筛（同为 auto-annotate 训练数据） | 只在切回该类时有影响 | 不再是默认，方案定型后随旧实现一并删除 |
