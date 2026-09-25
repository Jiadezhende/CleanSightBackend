# 离线段配置收缩：`offline` 块只留 `class` + `params`，CLEAN 默认启用

> **变更状态**：生效中（2026-09-25）
> **知识库**：待沉淀

## 概述

`inference_config.yaml` 各 stage 的 `offline` 块删去 `name` / `subscribes`，只留 `class` + `params`；
离线配置不拆独立文件；producer 改取模型类名，重跑替换该 step 全部分段；CLEAN 离线默认启用；
Runner 新增 `OfflineRunSpec.strict`（作业服务严格路由），CLI 默认不开、保留 MOCK 回落。
影响 `stage_factory.py` / `offline/` / `config/inference_config.yaml`，同批修掉 CLEAN 调试产物序列化的 P1。

## 变更背景

原 `offline` 块形如：

```yaml
offline:
  name: clean_offline                        # = TemporalSegment.producer
  subscribes: [clean_large, clean_small]     # 须 ⊆ 同 stage detector 名
  class: ...CleanMSTCNBiLSTMSegmenter
  params: { model_path: ... }
```

| 编号 | 问题 | 风险 |
|------|------|------|
| #1 | `subscribes` 不被消费：CLEAN 特征工程遍历 `by_source` 全部流、按 `class_name` 归类（`_collect_object_arrays`），`BrushRulesSegmenter` 同样看全部流；它只在 Runner 里用于「订阅流无数据则跳过」 | 配置项与行为脱钩，改了不生效 |
| #2 | `name` 是人造身份：一个 stage 至多一个离线模型，producer 只用于重跑时替换旧分段 | 换模型重跑后结果仍标 `clean_offline`，看不出是哪个模型产出的 |
| #3 | CLEAN 默认 `offline: {}`，唯一启用的是 MOCK，靠 `resolve_stage` 把 `--step-id -1` 回落命中 | 接入 admin 页手动触发后，未配 stage 的真实数据会被 MOCK 分段写脏 |

触发来源：离线推理接入 admin 页（手动幂等启动 + 结果可视化）的架构评审。

## 方案详情

### 全景：配置 → 工厂 → Runner → facts

```text
inference_config.yaml  stages.<step_id>.offline = {class, params}     缺省 / {} = 该 stage 不可跑
        │
        ▼ StageFactory.create_offline_segmenter(stage_key[, override_class])
  cls = _import_class(class)；segmenter = cls(**params)                不再注入 name / subscribes
        │
        ▼ OfflineRunner.run(OfflineRunSpec(task_id, step_id, strategy, strict))
  strict=True  → stage_key = str(step_id)，未配 / offline 空 → skipped（不回落 MOCK）
  strict=False → stage_key = resolve_stage(step_id)，未配回落 MOCK（CLI 开发回环）
  read_detections 为空 → skipped
  facts = segmenter 产出；producer = type(segmenter).__name__
  read_temporal → 丢掉全部 TemporalSegment、保留 TemporalEvent → 追加本次 → write_temporal
```

| 步骤 / 部件 | 落在哪 | 详见 |
|------------|--------|------|
| 配置位置与块形状 | `config/inference_config.yaml` | §1 §2 |
| 工厂读取与校验 | `app/services/inference/stage_factory.py` | §2 |
| producer 与替换规则 | `offline/segmenter.py`、`offline/runner.py` | §3 |
| 默认启用 | `config/inference_config.yaml` | §4 |
| 路由语义 | `offline/runner.py`（`OfflineRunSpec.strict`） | §5 |

### 方案选型：留在 `inference_config.yaml`，不拆独立文件

| 方案 | 代价 / 影响面 | 结论 |
|------|--------------|------|
| A（采用）留在各 stage 的 `offline` 块 | 与在线共文件 | 收缩后每 stage 只剩 `class` + `params`；`alias` 共用；一个业务的在线与离线在同一处可见 |
| B 独立 `config/offline_config.yaml` | 每 stage 要复制一份 `alias`；多一个加载器 | 否：拆分的唯一实据「离线配错拖垮在线」只剩 YAML 语法错一种，而语法错在本文件任何位置都同样致命。在线进程从不实例化 `offline` 块的类，类路径错、权重缺失都只在离线运行时暴露 |

### 1. 块形状

```yaml
  "2":
    alias: CLEAN
    detectors: [...]
    rules: [...]
    offline:                         # 离线段（stage 粒度）：class 指定离线模型，缺省/空块 = 不可跑
      class: app.services.inference.offline.impl.clean.CleanMSTCNBiLSTMSegmenter
      # 换 class 须同时换 model_path（权重与特征 recipe 一一对应）：
      #   ...CleanASFormerSegmenter / ...CleanBiGRUSegmenter
      params:
        model_path: ${CLEANSIGHT_MODEL_PATH:./app/data}/clean-offline-mstcn-bilstm.pt
        min_duration_s: 0.2          # 短于此时长的动作段丢弃

  MOCK:
    alias: MOCK
    ...
    offline:
      class: app.services.inference.offline.impl.mock.BrushRulesSegmenter
      params: { label: mock_action, min_frames: 1 }
```

进配置的只有两类：选哪个模型（`class`）、部署相关或会调的值（`model_path`、`min_duration_s`）。
`fps` 由 ts 差分得到，帧分辨率由帧自带，均不进配置。

### 2. 删 `name` / `subscribes`

- **`subscribes` 删除**：见背景 #1。模型需要哪路特征由模型类声明，与 checkpoint 同源。
  Runner 的跳过判据从「订阅流缺数据」改为「`read_detections` 为空」。
- **工厂校验只剩「`class` 必填、可导入」**：原 `subscribes ⊆ detector 名`、`params` 不得含 `name` / `subscribes`
  的保留字校验一并删除。`override_class`（CLI `--strategy`）保留，覆盖 `class`、沿用 `params`。

### 3. producer = 模型类名；替换该 step 全部分段

- `OfflineSegmenter.__init__` 去掉 `name` / `subscribes` 两参（基类不再定义 `__init__`）；
  `name` 为只读 property，返回 `type(self).__name__`。`BrushRulesSegmenter` 与 CLEAN 三个模型构造签名同步。
- Runner 替换规则由「丢自己 producer 的 TemporalSegment」改为「丢全部 TemporalSegment、保留 TemporalEvent」
  （`_replace_own_segments` → `_replace_segments`，去掉 `producer` 参数）。
  一个 stage 至多一个离线模型，换模型重跑时旧模型的结果整体被替换，不会与新结果并存。
- `_validate` 的 `f.producer == segmenter.name` 校验保留，只是右值来源变了。
- `CleanSegmenter` 是 `CleanMSTCNBiLSTMSegmenter` 的别名，producer 取到的是 `CleanMSTCNBiLSTMSegmenter`。

将来要在同一 step 并排对比多个模型时：`offline` 块改列表、替换规则改回按 producer，`temporal.jsonl` 的格式不动。

### 4. CLEAN 离线默认启用

原先 `{}` 的理由是「权重不在时别让生产误启用」。离线作业与在线进程隔离，权重缺失只让该 step 的离线作业
失败（`FileNotFoundError`），不影响在线。故默认启用，部署机器需放好 `clean-offline-mstcn-bilstm.pt`。

LEAK 保持 `offline: {}`（无离线模型）。

### 5. 路由语义按入口分

| 入口 | 未配 `offline` 的 step | 实现 |
|------|----------------------|------|
| 作业服务（admin 页 / 后台触发） | skipped，不回落 MOCK | `OfflineRunSpec(strict=True)`：`stage_key = str(step_id)`，未命中或 offline 为空即 skipped |
| CLI | 经 `resolve_stage` 回落 MOCK | `strict` 默认 `False`，行为不变；CLI 不暴露该开关 |

作业服务尚未实现，`strict` 暂无生产调用方；先落在 `OfflineRunSpec` 上（而非 `run()` 参数），
与 `strategy` 同属「一次运行的输入」，作业服务接入时直接构造 spec 即可，且有用例锁定语义。
「不出现在可跑清单」属作业服务职责，本批不涉及。

### 6. 保留项（不改动）

- `StageConfig.offline` 字段、`resolve_stage`、`FALLBACK_STAGE`。
- CLI 的 `--strategy`（覆盖 `class`）。
- `temporal.jsonl` / `offline_debug.json` 的落盘格式与位置。

### 7. 同批修复：CLEAN 调试产物序列化（P1）

`clean.py` `_CleanTorchSegmenter.segment()` 组 `debug_result` 时调 `TemporalSegment.to_json()`，该方法已随 0921
事实契约升格删除——带权重的 CLEAN 离线运行会在写 facts 前抛 `AttributeError`。改为 `dataclasses.asdict(s)`。
原测试只覆盖「无 model_path 硬失败」分支，故一直全绿；新增用例打桩 `_predict_with_model` 走通该路径
（已确认在旧代码上以 `AttributeError: 'TemporalSegment' object has no attribute 'to_json'` 失败）。

## 变更效果

| 维度 | 变更前 | 变更后 |
|------|--------|--------|
| `offline` 块字段 | `name` / `subscribes` / `class` / `params` | `class` / `params` |
| producer | 配置里的 `name`（如 `clean_offline`） | 模型类名（如 `CleanMSTCNBiLSTMSegmenter`） |
| 跳过判据 | 任一订阅 source 无数据 | `read_detections` 为空 |
| 重跑替换范围 | 同 producer 的 TemporalSegment | 该 step 全部 TemporalSegment（TemporalEvent 保留） |
| CLEAN 离线 | 默认不启用 | 默认启用 |
| 未配 stage | 一律回落 MOCK | `strict=True` 不回落；CLI（默认）回落 MOCK |
| 带权重 CLEAN 运行 | `AttributeError`（P1） | 正常落 facts + `offline_debug.json` |

### 改动文件

- `config/inference_config.yaml`：三处 offline 块与顶部说明注释
- `app/services/inference/stage_factory.py`：`create_offline_segmenter`
- `app/services/inference/offline/segmenter.py`、`offline/runner.py`、`offline/impl/mock.py`、`offline/impl/clean.py`
- `tests/test_offline_pipeline.py`
- `README.md`、`.claude/skills/infer-workflow/references/yaml-config.md`

## 自测结果

- `pytest tests/`：改动前 858 passed / 8 skipped → 改动后 **863 passed / 8 skipped**。
  `test_offline_pipeline.py` 改写与新增：`class` 缺失 / 不可导入（模块不存在、类不存在）两档；`override_class`；
  producer = 类名；部分 source 有数据不再 skip；换模型重跑旧类名分段被整体替换且 TemporalEvent 保留；
  `strict` 四档（未配 step 不回落、offline 空、命中正常、非 strict 回落 MOCK）；CLEAN 带权重路径（打桩前向）
  的 `debug_result` 与经 Runner 落 `offline_debug.json`。
- CLI 回环（临时 `CLEANSIGHT_STORAGE_DIR`，dummy DB/告警环境变量，种 4 帧 `mock` 检测）：
  `run --task-id 1 --step-id -1` → `completed producer=BrushRulesSegmenter segment_count=2`；
  `query` 读回两段 `producer=BrushRulesSegmenter`、`label=mock_action`（[1.0, 2.0]、[4.0, 4.0]）。
- 真实配置加载：`create_offline_segmenter("2")` 得 `CleanMSTCNBiLSTMSegmenter`，`"1"` 得 `None`；主进程未 import torch。

## 遗留风险 / 后续任务

| 风险 / 待办 | 影响 | 处理计划 |
|------------|------|---------|
| 部署机器缺 CLEAN 离线权重 | CLEAN step 的离线作业全部失败 | 随部署物料分发；`/deploy` skill 的 runtime-config 已写「六份 `.pt` 见 `model_path` 行」，本批后仍为六份，无需改 |
| 盘上旧 facts 的 producer 是 `clean_offline` / `mock_offline` | 与新类名不同 | 新替换规则丢全部 TemporalSegment，重跑一次即清掉，无需迁移 |
| 作业服务未实现 | `strict=True` 暂无生产调用方 | 作业服务接入时以 `OfflineRunSpec(strict=True)` 调用，并负责「可跑清单」过滤 |
| ~~P1：`clean.py` `segment()` 调 `TemporalSegment.to_json()`~~ | ~~带权重 CLEAN 离线运行抛 `AttributeError`~~ | **已修**（§7） |
| 特征方案（Featurizer / `FeatureSequence`）接缝 | 「模型类声明所需特征」依赖它 | 契约侧另起记录 |

## 合入说明（2026-09-25）

本批与 [20260925_TEMPORAL_TYPE_RENAME](20260925_TEMPORAL_TYPE_RENAME.md) 并行开发，变基到其之上合入：文中与代码里的时序类型已统一为新名（`TemporalSegment` / `TemporalEvent` / `read_temporal` / `write_temporal` / `temporal.jsonl`）。变基后全量 `pytest tests/` 863 passed、8 skipped（跳过项均需 cv2 与项目自带 ffmpeg）。
