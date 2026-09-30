> 更新时间：2026-09-30
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 新增检测任务指南

推理采用**流处理框架**：检测点拆成两粒度——无状态 **Detector**（流源，多 run 共享）+ per-run **Operator**（流算子，analyze+judge 合并）。新增检测点只需各加一个子类 + YAML 各加一行。可用 `/infer-workflow` skill 生成代码框架。

**落点（一文件一基类）**：Detector 子类写 `online/detection/impl/<业务>.py`，Operator 子类写 `online/temporal/impl/<业务>.py`，可选离线 Segmenter 写 `offline/impl/<业务>.py`（均在 `app/services/inference/` 下）；三者同名文件，业务聚合由 config stage 绑定表达（各契约包顶层只放基类+框架，`impl/` 放业务实现）。online 与 offline 互不 import，两边都要用的纯函数放推理包顶层（如 `resample.py`）。

## 新增 Detector（流源）

继承 `Detector`（`online/detection/detector.py`），YOLO 类优先继承 `YOLODetector`（复用模型惰性加载、batch predict、输出适配、CUDA 异常转换）。职责：

- 设唯一 `name`——即该 detector 产出的**流名**（`FrameDetection.by_source` 的 key，Operator 用它 `subscribes`）。
- `infer_batch(frames, timestamps) → List[DetectorOutput]`（**唯一推理入口**，无单帧 `infer()`）。`timestamps[i]` 是帧捕获真值锚点（源自 `Frame.timestamp`），实现须原样写入 `frames[i]` 对应的 `DetectorOutput.timestamp`，**不得自造时间戳**——同帧各流按它装进同一个 `FrameDetection.by_source`，算子用 `FrameDetection.ts` 裁窗、用 `DetectorOutput.timestamp` 推进游标，二者须同源同值。YOLO 子类已在 `YOLODetector.infer_batch` 实现（整批失败逐帧返回 `success=False` 的空结果、仍保留各帧 ts）。
- `prepare_visualization_data(output: DetectorOutput) → RenderSpec`（`RenderSpec` 在 `online/render.py`；可视化用固定渲染器 `FixedVisualizer`）。
- **不持 per-run 状态**。

`class_name` 直接取自模型 `result.names`，不做归一化——匹配字符串必须与训练类别名严格一致。`DetectorOutput`（`app/types/detection.py`，含 `boxes: List[DetBox]`）是统一检测契约，不要为单点往里加领域字段（如 `xxx_detected/xxx_count`）；单框派生量放 `DetBox.extra`（不落盘），时序统计交给 Operator。

## 新增 Operator（流算子）

继承 `Operator`（`online/temporal/operator.py`），per-run 独立实例（可持 ByteTrack、计数器、锁存等状态于 `self._sm`）。职责：

- `name`（规则名）与 `subscribes`（**显式必填**的输入流名列表——即所订阅 Detector 的 `name`；不提供隐式默认，缺失 fail-fast）。
- `window_seconds`：感受野（秒），`analyze` 内用 `_clip()` 裁窗。
- `analyze(windows: List[FrameDetection]) → None`：读订阅流，推进 `self._sm`。
- `judge() → (List[str], List[Alarm])`：读 `_sm`，返回（叠字文本，告警）。
- 如有结算逻辑 override `finalize() → List[Alarm]`（任务终止时收集）。

多个 Operator 可订阅同一 Detector；每 Operator 持自己的 `_sm`。`analyze` 收到的 `windows` 是帧级 `List[FrameDetection]`（按 ts 升序，多流已对齐进 `by_source`，算子直接读，无需自行 zip）；工具方法 `primary_window(windows) → List[DetectorOutput]`（裁到感受野后投影首个订阅流的逐帧输出）。

### 时序模型算子（TemporalOperator）

接入动作识别/序列模型（GRU/Transformer/MS-TCN 等）时继承 `TemporalOperator`（`online/temporal/operator.py`，`Operator` 子基类），多带 `model_path` / `objects` / `actions` / `model_input_fps` 四参：惰性 `torch.jit.load`（双检锁、缺文件 `FileNotFoundError`、加载失败锁存，钉 CPU），`infer(features) → logits`。

- **入模帧率是模型契约**：`model_input_fps` 必填，须等于训练帧率；构造期校验 >0 且 ≤ `settings.inference_fps`（重采样只能降采样，契约帧率高于检测采样率即拒）。配错帧率不崩、静默误分类，所以必须显式配、构造期暴露。
- 子类在 `analyze` 内**先 `_resample_by_ts`**（委托共享 `app/services/inference/resample.py::resample_by_ts`，按帧 ts 相位网格抽稀）再把订阅流窗口适配成 `(T, feature_dim)` 张量后 `infer`，把预测存进 `_sm`，`judge` 读 `_sm` 出 overlay/告警。新帧门等游标仍基于完整窗口，重采样只决定喂模型的时间密度。
- 参考 `CleanOperator`（`online/temporal/impl/clean.py`）：`_adapt_to_features` 把每帧多流检测折成 `(num_objects×6)`，异常帧留全零行保持时间轴对齐。`class_name → object_id` 经 `objects` 映射，仍须与训练类别名严格一致。YAML `params` 里配 `model_path`/`objects`/`actions`/`model_input_fps`（见 CLEAN `clean_monitor`）。
- 新增时序算子接入可用 `/temporal-review` skill 走审查清单。

## 配置 YAML

`config/inference_config.yaml` 对应 stage 下，`detectors[]` 加流源、`rules[]` 加算子：

```yaml
stages:
  "1":
    alias: LEAK
    detectors:
      - name: example
        class: app.services.inference.online.detection.impl.example.ExampleDetector
        params: { model_path: ..., conf_threshold: 0.1, enabled: true }
    rules:
      - name: example_rule
        subscribes: [example]      # 必填，值 = 上面 detector.name
        realtime: true             # true 纳入 signals_10s；false 为结算告警
        class: app.services.inference.online.temporal.impl.example.ExampleOperator
        params: { window_seconds: 3.0, ... }
    offline: {}                    # 可选：{class, params}；缺省/空块 = 该 stage 不可跑离线
```

`StageFactory` 按 YAML 建共享 Detector 实例 + Operator specs，并构建 `_TASK_METRIC_MAP`（仅 `realtime:true` 流）与 `_STAGE_ALIAS_MAP`（`stage.alias`）。**配置错即启动失败**：任一 detector/operator 导入或构造失败、rule 缺 `class`/`subscribes`，后端 lifespan 直接起不来，不会静默少一个组件。

## Stage 路由

stage 主键 = step_id 字符串，`resolve_stage` 恒等路由，**无兜底 stage**：YAML 未定义或定义了但无 detector 的 step，`/api/start` 直接 400（`ValidationError`），不会路由到别的实现。新增洗消步骤 = 加一个 stage 键（至少一个 detector 才生效）；给已有 stage 加检测点只改 YAML + 新增类。`rules: []` 的 stage 不建 Operator/Actor（纯检测框可视化）。

## 新增离线 segmenter（可选）

离线段独立于在线链路（独立子进程跑，不接 CQ/告警），run 结束后对整段检测序列做全序列分割。新增 = 往 `offline/impl/` 加一个自包含单文件的 `OfflineSegmenter` 子类 + 目标 stage YAML 的 `offline` 块填 `class` + `params`（只有这两个键；`params` 原样作构造参数）。

- **身份**：`name` = 类名，自动成为产出 `TemporalSegment.producer`，子类不要自定义。
- **接口**：`preprocess(frames: Sequence[FrameDetection]) → 模型输入`（基类不做默认特征工程）与 `segment(model_input) → List[TemporalSegment]`（每条 `producer` 须等于 `self.name`，时间为帧捕获墙钟 ts）；产逐帧概率的模型可 override `label_probs() → LabelProbs | None`，Runner 先于事实落 `label_probs.npz` 供可视化。
- **Runner 统一收尾**：校验（producer / 有限数 / start≤end / conf 值域，任一非法整批失败）后 read-merge-write `temporal.jsonl`，替换该 run 的全部 `TemporalSegment`、保留 `TemporalEvent`——一个 stage 至多一个离线模型。
- **约定**：策略是纯算法，不碰 `app.storage` / CQ / DB，`frames` 只读。需要按训练帧率入模的模型用 `resample_by_ts(frames, fps, strict=True)`（检测帧率不够即报错，不静默放行）。权重类模型只读单个 `.pt`，网络结构 / window 写死在策略类；`strict=True` 加载；无 `model_path` 应硬失败（`ValueError`）而非规则降级。换策略 = 改 YAML 的 `class` 与 `params`（权重与特征 recipe 一一对应，须一起换）。
- **本地回环**：测试用 `tests/doubles.py::BrushRulesSegmenter`（纯规则、无权重）写进注入的 config；验证入口是 CLI `run` 或 admin「运行离线推理」。
- CLEAN 四个模型集中在 `offline/impl/clean.py`（默认 `CleanNodepGRUSegmenter`，备选 MS-TCN+BiLSTM / ASFormer / BiGRU），特征工程为模块级纯函数、多态只在各子类 override `preprocess`；细节见 [SERVICE_INFERENCE.md](SERVICE_INFERENCE.md)。

## 告警 metric

`AlarmMetric` 由 Operator 产 `Alarm` 时**显式设定**（`alarm.metric`），非下游文本反推。新增流名后确认 `_TASK_METRIC_MAP` 是否需补枚举/映射测试。

## 测试建议

- Detector 输出 `DetectorOutput` 格式正确、`timestamp` 原样回写。
- Operator 上升沿触发 / 恢复 / 结算逻辑正确。
- YAML 可被 `StageFactory` 加载。
- 目标 step 的 `resolve_stage` 恒等返回；未配置 step 抛 `ValidationError`（400）。
- 新增离线策略：`require_offline` 通过、Runner 产出 `producer` = 类名。
- `/task/message/{task_id}` 的 signals 含新 metric。

## 代码来源

- `app/services/inference/online/detection/detector.py`
- `app/services/inference/online/temporal/operator.py`（`Operator` + `TemporalOperator`）
- `app/services/inference/online/temporal/impl/clean.py`（`CleanOperator` 时序算子示例）+ `app/services/inference/online/detection/impl/clean.py`（检测器）
- `app/services/inference/offline/{segmenter,runner}.py`、`offline/impl/clean.py`
- `app/services/inference/{stage_factory,config,resample}.py`
- `app/services/inference/online/service.py`（`resolve_stage`）
- `app/types/detection.py`、`app/types/temporal.py`
- `config/inference_config.yaml`
- `tests/test_inference_stage_routing.py`、`tests/test_offline_pipeline.py`、`tests/doubles.py`
