> 更新时间：2026-07-06
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 新增检测任务指南

推理采用**流处理框架**：检测点拆成两粒度——无状态 **Detector**（流源，多 run 共享）+ per-run **Operator**（流算子，analyze+judge 合并）。新增检测点只需各加一个子类 + YAML 各加一行。可用 `/infer-workflow` skill 生成代码框架。

## 新增 Detector（流源）

继承 `Detector`（`detection/detector.py`），YOLO 类优先继承 `YOLODetector`（复用模型惰性加载、batch predict、输出适配、CUDA 异常转换）。职责：

- 设唯一 `name`——即该 detector 产出的**流名**（slide_window 的 key，Operator 用它 `subscribes`）。
- `infer(frame, context) → FrameDetections`（或 override `infer_batch`）。
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

多个 Operator 可订阅同一 Detector；每 Operator 持自己的 `_sm`。工具方法：`primary_window()`（首个订阅流）、`_zip_by_ts()`（多流按时间戳内连对齐）。

## 配置 YAML

`config/inference_config.yaml` 对应 stage 下，`detectors[]` 加流源、`rules[]` 加算子：

```yaml
stages:
  "1":
    alias: LEAK
    detectors:
      - name: example
        class: app.services.inference.workflows.example.ExampleDetector
        params: { model_path: ..., conf_threshold: 0.1, enabled: true }
    rules:
      - name: example_rule
        subscribes: [example]      # 必填，值 = 上面 detector.name
        realtime: true             # true 纳入 signals_10s；false 为结算告警
        class: app.services.inference.workflows.example.ExampleOperator
        params: { window_seconds: 3.0, ... }
    offline: {}                    # 占位，未实现
```

`StageFactory` 按 YAML 建共享 Detector 实例 + Operator specs，并构建 `_TASK_METRIC_MAP`（仅 `realtime:true` 流）与 `_STAGE_ALIAS_MAP`（`stage.alias`）。

## Stage 路由

stage 主键 = step_id 字符串（`resolve_stage` 恒等路由，未知回落 `MOCK`）。新增洗消步骤 = 加一个 stage 键；给已有 stage 加检测点只改 YAML + 新增类。`rules: []` 的 stage 不建 Operator/Actor（如 CLEAN 仅检测框可视化）。

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
- `app/services/inference/temporal/operator.py`
- `app/services/inference/stage_factory.py`
- `app/services/inference/manager.py`
- `app/services/inference/models.py`
- `app/domain/detection.py`
- `config/inference_config.yaml`
- `tests/test_inference_stage_routing.py`
