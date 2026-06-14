---
name: infer-workflow
description: 用于 CleanSightBackend 推理 workflow 的新增或修改，包括 Detector、YOLODetector、TemporalAnalyzer、滑动窗口、events、alarms、YAML 装配，以及 MS-TCN 这类长窗口时序模型接入。
---

# 推理 Workflow 开发规范

## 用途

这个 skill 用来实现或解释 CleanSightBackend 的检测/推理 workflow。项目里的标准链路是：

```text
Detector -> DetectionOutput -> slide_window -> TemporalAnalyzer -> events / alarms
```

本文件是 `.claude/skills/infer-workflow/` 的 Codex 中文转换版。需要代码模板和字段细节时，
可以继续阅读原始参考文件：

- `.claude/skills/infer-workflow/SKILL.md`
- `.claude/skills/infer-workflow/references/data-models.md`
- `.claude/skills/infer-workflow/references/yaml-config.md`
- `.claude/skills/infer-workflow/references/templates.md`

## 触发场景

当用户提到以下任务时，应该先阅读本文件：

- 新增检测任务或 workflow。
- 新增 Detector 或 TemporalAnalyzer。
- 接入新的 YOLO 检测模型。
- 接入纯算法检测任务。
- 增加实时告警或结算式告警。
- 接入 MS-TCN、动作分割、行为识别等长窗口时序模型。
- 解释滑动窗口、events、alarms、`inference_config.yaml`。

## 核心模型

每个推理任务通常由两部分组成：

- `Detector`：无状态，多 client 共享，负责单帧或批量推理，以及可视化数据准备。
- `TemporalAnalyzer`：有状态，每个 client 一个实例，读取滑动窗口，生成 events 和 alarms。

两类输出语义不同：

- `events: List[str]`：给前端 overlay 或调试展示的临时事件，不入库。
- `alarms: List[AlarmInfo]`：正式告警，会进入持久化链路。

## 实现规则

- YOLO 模型优先继承 `YOLODetector`。
- 纯算法任务可以直接继承 `Detector`。
- YOLO 热路径优先实现 `infer_batch()`，失败时 fallback 到单帧 `infer()`。
- batch 路径和 fallback 单帧路径的业务字段赋值必须保持一致。
- 必须实现 `prepare_visualization_data()`。
- 可视化颜色使用 BGR，不是 RGB。
- Analyzer 的所有跨帧状态放在 `self._sm`。
- 涉及计数、追踪、累计、喂时序模型时，必须使用 `last_ts` 这类游标。
- `get_slide_window()` 是非破坏性读取，连续 tick 会有大量重复帧。
- 指标窗口应由 Analyzer 自己维护，不要直接把全局 slide window 长度当业务窗口。
- 实时告警需要上升沿锁存，避免持续异常时重复刷告警。
- 结算式告警放在 `finalize()`，并通常在 YAML 中设置 `realtime: false`。

## name 和滑动窗口绑定

原始规范默认：

```text
Detector.name == TemporalAnalyzer.name == YAML model name
```

因为时序线程默认会读取：

```text
slide_window[name]
```

当前项目为了支持链式模型，也可以使用一个明确的例外：Analyzer 通过
`source_task_name` 读取另一个 Detector 的窗口。例如 MS-TCN 阶段识别链路：

```text
clean_tool Detector -> slide_window["clean_tool"] -> mstcn_phase Analyzer
```

使用这个例外时，需要在文档或代码说明里明确写清楚，普通 workflow 仍然优先使用同名规则。

## YAML 装配

workflow 真正生效的位置是：

```text
config/inference_config.yaml
```

典型结构：

```yaml
stages:
  CLEAN:
    models:
      - name: clean_tool
        realtime: false
        class: app.services.inference.workflows.clean_tool.CleanToolDetector
        analyzer_class: app.services.inference.workflows.mstcn_phase.MSTCNPhaseAnalyzer
        params:
          model_path: ./weights/best.pt
          conf_threshold: 0.25
          iou_threshold: 0.45
          enabled: true
        analyzer_params:
          name: mstcn_phase
          source_task_name: clean_tool
```

如果项目依赖 workflow 包导出类，通常还需要更新：

```text
app/services/inference/workflows/__init__.py
```

## 数据模型速查

字段以 `app/services/inference/data_models.py` 为准：

```python
Detection(bbox=[x1, y1, x2, y2], confidence=0.9, class_id=0, class_name="...")
DetectionOutput(detections=[...], metadata={...}, timestamp=..., success=True)
AlarmInfo(alarm_type=..., alarm_level="high", alarm_message="...", metadata={...})
VisualizationData(type=..., items=[...], status_text="...", status_color=(0, 255, 0))
VisItem(bbox=[...], label="...", confidence=0.9, color=(0, 0, 255))
```

不要手动给 `AlarmInfo` 填 `alarm_mode` 或 `alarm_metric`，这些由 manager 在持久化时补充。

## 模板选择

- 模板 A：YOLO 模型 + 实时告警，例如 bubble。
- 模板 B：无模型或纯算法任务。
- 模板 C：实时只展示，任务结束时结算告警。
- 模板 D：长窗口、低频时序模型，例如动作分割、行为识别、MS-TCN。

对于模板 D，优先让 Detector 或 backbone 生成紧凑特征，再由 Analyzer 自己维护特征窗口。
不要在 Analyzer 里缓存长时间原始帧，也不要为了单个长窗口模型去调大全局 slide window。

## 验证命令

推荐先做定向验证：

```bash
source .venv/bin/activate
python -m py_compile app/services/inference/workflows/<new_file>.py
python -m pytest tests/<targeted_test>.py -q
```

完整链路至少确认：

- `inference_config.yaml` 能加载并创建 Detector / Analyzer。
- Detector smoke test 能返回 `DetectionOutput`。
- Analyzer smoke test 能返回预期 `events`。
- `ClientTemporalActor` 能把 temporal events 合并到 `latest_temporal`。
- 在数据库可用时，integration test 能创建/读取 task 并推流。

## Codex 转换说明

这个 skill 没有必须依赖 Claude 专属工具的步骤。原始 `.claude` 里的模板可以继续作为
项目文档读取。Codex 执行相关任务时，应先读本文件，再按需要打开原始参考文件确认字段和模板。
