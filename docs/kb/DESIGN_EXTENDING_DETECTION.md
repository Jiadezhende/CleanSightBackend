> 更新时间：2026-05-24
> 依据来源：代码分析
> 可信级别：以当前仓库代码、配置、测试为准；旧 docs 仅作待核验参考

# 新增检测任务指南

当前推理架构把检测任务拆成无状态 Detector 和 per-client TemporalAnalyzer。

## 新增 Detector

新增类应继承：

- `Detector`
- 或更常见的 `YOLODetector`

职责：

- 设置唯一 `name`。
- 执行单帧或 batch 推理，返回 `DetectionOutput`。
- 实现 `prepare_visualization_data()`，返回 `VisualizationData`。
- 不保存 per-client 状态。

YOLO 类任务优先继承 `YOLODetector`，复用模型惰性加载、batch predict、输出适配和 CUDA 异常转换。

## 新增 TemporalAnalyzer

新增类继承 `TemporalAnalyzer`。

职责：

- `name` 必须与对应 Detector.name 一致。
- 在 `__init__` 初始化 `self._sm`。
- `analyze_temporal(window)` 读取滑动窗口，更新状态机，返回 `(events, alarms)`。
- 如有结算逻辑，override `finalize()`。

Analyzer 每个 client 独立实例化，可以持有 ByteTrack、计数器、锁存状态等 per-client 状态。

## 配置 YAML

在 `config/inference_config.yaml` 对应 stage 下新增 model：

```yaml
stages:
  LEAK:
    models:
      - name: example
        class: app.services.inference.workflows.example.ExampleDetector
        analyzer_class: app.services.inference.workflows.example.ExampleAnalyzer
        params:
          enabled: true
        analyzer_params:
          threshold: 1.0
```

`StageFactory` 会按 YAML 创建共享 Detector 实例和 Analyzer spec。

## Stage 路由

如果是新增洗消步骤，需要更新 `InferenceManager._STEP_TO_STAGE`，把新的 `current_step` 映射到 stage。

如果只是给已有 stage 增加检测点，只改 YAML 和新增类即可。

## 告警 metric

全局 task_name 到 `AlarmMetric` 的映射由 `StageFactory.build_task_metric_map()` 初始化。新增任务名后，应确认前端消息和告警 metric 是否需要新增枚举或映射测试。

## 测试建议

- Detector 输出 `DetectionOutput` 格式正确。
- Analyzer 上升沿触发、恢复、结算逻辑正确。
- YAML 可以被 `StageFactory` 加载。
- `set_task()` 能路由到目标 stage。
- `/task/message/{task_id}` 中 signals 包含新 metric。

## 代码来源

- `app/services/inference/workflows/detector.py`
- `app/services/inference/workflows/analyzer.py`
- `app/services/inference/stage_factory.py`
- `app/services/inference/core/manager.py`
- `app/services/inference/data_models.py`
- `config/inference_config.yaml`
- `tests/test_inference_stage_routing.py`

