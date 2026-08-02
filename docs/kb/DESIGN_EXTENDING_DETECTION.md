> 更新时间：2026-07-21
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 新增检测任务指南

推理采用**流处理框架**：检测点拆成两粒度——无状态 **Detector**（流源，多 run 共享）+ per-run **Operator**（流算子，analyze+judge 合并）。新增检测点只需各加一个子类 + YAML 各加一行。可用 `/infer-workflow` skill 生成代码框架。

**落点（一文件一基类）**：Detector 子类写 `detection/impl/<业务>.py`，Operator 子类写 `temporal/impl/<业务>.py`，可选离线 Segmenter 写 `offline/impl/<业务>.py`；三者同名文件，业务聚合由 config stage 绑定表达（各契约包顶层只放基类+框架，`impl/` 放业务实现）。

## 新增 Detector（流源）

继承 `Detector`（`detection/detector.py`），YOLO 类优先继承 `YOLODetector`（复用模型惰性加载、batch predict、输出适配、CUDA 异常转换）。职责：

- 设唯一 `name`——即该 detector 产出的**流名**（slide_window 的 key，Operator 用它 `subscribes`）。
- `infer_batch(frames, timestamps) → List[FrameDetections]`（**唯一推理入口**，无单帧 `infer()`）。`timestamps[i]` 是帧捕获真值锚点（源自 `Frame.timestamp`），实现须原样写入 `frames[i]` 对应的 `FrameDetections.timestamp`，**不得自造时间戳**——下游 `_zip_by_ts` 按同帧 ts 精确相等对齐多流，ts 不等会漏帧。YOLO 子类已在 `YOLODetector.infer_batch` 实现（整批失败逐帧返回 error 结果、仍保留各帧 ts）。
- `prepare_visualization_data(output) → RenderSpec`（可视化用固定渲染器 `FixedVisualizer`）。
- **不持 per-run 状态**。

`class_name` 直接取自模型 `result.names`，不做归一化——匹配字符串必须与训练类别名严格一致。`FrameDetections`（`app/domain/detection.py`，含 `Detection` 列表）是统一检测契约，不要为单点往里加领域字段（如 `xxx_detected/xxx_count`）；派生量放 `Detection.extra`，时序统计交给 Operator。

## 新增 Operator（流算子）

继承 `Operator`（`temporal/operator.py`），per-run 独立实例（可持 ByteTrack、计数器、锁存等状态于 `self._sm`）。职责：

- `name`（规则名）与 `subscribes`（**显式必填**的输入流名列表——即所订阅 Detector 的 `name`；不提供隐式默认，缺失 fail-fast）。
- `window_seconds`：感受野（秒），`analyze` 内用 `_clip()` 裁窗。
- `analyze(windows: Dict[str, List[FrameDetections]]) → None`：读订阅流窗口，推进 `self._sm`。
- `judge() → (List[str], List[Alarm])`：读 `_sm`，返回（叠字文本，告警）。
- 如有结算逻辑 override `finalize() → List[Alarm]`（任务终止时收集）。

多个 Operator 可订阅同一 Detector；每 Operator 持自己的 `_sm`。工具方法：`primary_window()`（首个订阅流，裁到感受野后投影自身流的逐帧 `FrameDetections`）；`analyze` 收到的 `windows` 是帧级 `List[FrameFeature]`（多流已在写回口按 ts 对齐进 `by_source`，算子直接读，无需自行 zip）。

### 时序模型算子（TemporalOperator）

接入动作识别/序列模型（GRU/Transformer/MS-TCN 等）时继承 `TemporalOperator`（`temporal/operator.py`，`Operator` 子基类），多带 `model_path` / `objects` / `actions` 三参：惰性 `torch.jit.load`（双检锁、缺文件 `FileNotFoundError`、加载失败锁存），`infer(features) → logits`。子类在 `analyze` 内把订阅流窗口适配成 `(T, feature_dim)` 张量后 `infer`，把预测存进 `_sm`，`judge` 读 `_sm` 出 overlay/告警。参考 `CleanOperator`（`temporal/impl/clean.py`）：`_adapt_to_features` 把每帧多流检测折成 `(num_objects×6)`，异常帧留全零行保持时间轴对齐。`class_name → object_id` 经 `objects` 映射，仍须与训练类别名严格一致。YAML `params` 里配 `model_path`/`objects`/`actions`（见 CLEAN `clean_monitor`）。新增时序算子接入可用 `/temporal-review` skill 走审查清单。

## 配置 YAML

`config/inference_config.yaml` 对应 stage 下，`detectors[]` 加流源、`rules[]` 加算子：

```yaml
stages:
  "1":
    alias: LEAK
    detectors:
      - name: example
        class: app.services.inference.detection.impl.example.ExampleDetector
        params: { model_path: ..., conf_threshold: 0.1, enabled: true }
    rules:
      - name: example_rule
        subscribes: [example]      # 必填，值 = 上面 detector.name
        realtime: true             # true 纳入 signals_10s；false 为结算告警
        class: app.services.inference.temporal.impl.example.ExampleOperator
        params: { window_seconds: 3.0, ... }
    offline: {}                    # 占位，未实现
```

`StageFactory` 按 YAML 建共享 Detector 实例 + Operator specs，并构建 `_TASK_METRIC_MAP`（仅 `realtime:true` 流）与 `_STAGE_ALIAS_MAP`（`stage.alias`）。

## Stage 路由

stage 主键 = step_id 字符串（`resolve_stage` 恒等路由，未知回落 `MOCK`）。新增洗消步骤 = 加一个 stage 键；给已有 stage 加检测点只改 YAML + 新增类。`rules: []` 的 stage 不建 Operator/Actor（纯检测框可视化）。

## 新增离线 segmenter（可选）

离线段独立于在线链路（独立进程手动跑，不接 CQ/告警）。新增 = 往 `offline/segmenters/` 加一个自包含单文件的 `OfflineSegmenter` 子类 + 目标 stage YAML 的 `offline` 段填 `name`/`subscribes`/`class`（非空即启用，`{}` 或缺省=不启用）。子类实现 `preprocess(frames: Sequence[FrameFeature]) → 模型输入`（基类不做默认特征工程）与 `segment(model_input) → List[SegmentFact]`（每条 `source` 须等于策略 `name`）。`OfflineRunner` 统一校验并幂等写 `FactLedger`。约定：策略是纯算法，不碰 FeatureStore/FactLedger/CQ/DB；权重类模型 `strict=True` 加载并校验 `feature_version`/`feature_names` 一致，无权重应硬失败（`ValueError`）而非规则降级——本地无权重回环走 MOCK stage 的 `BrushRulesSegmenter`。CLEAN 三模型（MS-TCN+BiLSTM / ASFormer / BiGRU）集中在 `segmenters/clean.py`，特征工程为模块级纯函数、多态只在各子类 override `preprocess`。

## 告警 metric

`AlarmMetric` 由 Operator 产 `Alarm` 时**显式设定**（`alarm.metric`），非下游文本反推。新增流名后确认 `_TASK_METRIC_MAP` 是否需补枚举/映射测试。

## 测试建议

- Detector 输出 `FrameDetections` 格式正确。
- Operator 上升沿触发 / 恢复 / 结算逻辑正确。
- YAML 可被 `StageFactory` 加载。
- `resolve_stage` 能路由到目标 stage。
- `/task/message/{task_id}` 的 signals 含新 metric。

## 代码来源

- `app/services/inference/detection/detector.py`
- `app/services/inference/temporal/operator.py`（`Operator` + `TemporalOperator`）
- `app/services/inference/temporal/impl/clean.py`（`CleanOperator` 时序算子示例）+ `app/services/inference/detection/impl/clean.py`（检测器）
- `app/services/inference/offline/{segmenter,runner}.py`、`offline/segmenters/{clean,mock}.py`
- `app/services/inference/stage_factory.py`
- `app/services/inference/manager.py`
- `app/services/inference/models.py`
- `app/domain/detection.py`
- `config/inference_config.yaml`
- `tests/test_inference_stage_routing.py`
