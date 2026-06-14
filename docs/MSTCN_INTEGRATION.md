# MS-TCN 阶段识别接入说明

## 目标

本次接入把离线训练好的 MS-TCN++ 动作阶段识别模型接入现有实时推理链路的 `CLEAN` 阶段，用于输出清洗动作阶段事件：

```text
Idle / Long_Brushing / Short_Brushing
```

第一版只输出 temporal event，不产生正式告警，不改变 `LEAK` 阶段原有的 bubble / bending 告警逻辑。

## 接入后的链路

```text
CLEAN stage
  -> CleanToolDetector
  -> DetectionOutput
  -> slide_window["clean_tool"]
  -> MSTCNPhaseAnalyzer(source_task_name="clean_tool")
  -> window_to_mstcn_features()
  -> [20, T]
  -> MSTCNRuntime
  -> latest_temporal.events
```

这条链路遵循 `.claude/skills/infer-workflow/SKILL.md` 的核心拆分：

```text
Detector（无状态帧级推理） + TemporalAnalyzer（每 client 独立时序分析）
```

其中 `CleanToolDetector` 负责把视频帧转成检测结果，`MSTCNPhaseAnalyzer` 负责把一段
检测窗口转成 MS-TCN 阶段事件。

### 与标准 workflow 的差异

`infer-workflow` skill 中的默认绑定规则是：

```text
Detector.name == TemporalAnalyzer.name == YAML name
```

系统据此读取：

```text
slide_window[name]
```

本次 MS-TCN 接入使用了一个明确的链式模型例外：

```text
Detector.name = clean_tool
Analyzer.name = mstcn_phase
Analyzer.source_task_name = clean_tool
```

原因是 MS-TCN 不是直接消费原始帧，而是消费 `clean_tool` YOLO 的检测窗口。为此
`ClientTemporalActor` 支持：

```python
source_name = getattr(analyzer, "source_task_name", analyzer.name)
window = self._cq.get_slide_window(source_name)
```

旧 workflow 没有 `source_task_name` 时仍保持原来的同名读取方式。

### 事件与告警边界

当前 MS-TCN 只输出 `events`：

```text
mstcn_phase=<label> conf=<score>
```

它不会生成 `AlarmInfo`，因此不会进入 persistence 的正式告警链路。后续如果要让阶段识别影响最终业务结果，需要再增加业务规则，例如：

- 阶段顺序错误时生成实时 `AlarmInfo`
- 任务结束时通过 `finalize()` 检查阶段是否缺失
- 对阶段持续时间异常做结算式告警
- 增加独立 `FusionAnalyzer`，把 bubble / bending / mstcn_phase 组合成最终裁决

最终前端和可视化线程读取到的事件形如：

```text
mstcn_phase=Long_Brushing conf=0.91
```

## 新增代码

### `app/services/inference/workflows/clean_tool.py`

新增 `CleanToolDetector`，继承现有 `YOLODetector`。

职责：

- 加载 `./weights/best.pt`
- 检测 MS-TCN 特征提取所需的 4 类目标：
  - `Hand`
  - `Long_Brush_Head`
  - `Scope_Port`
  - `Short_Brush`
- 输出标准 `DetectionOutput`
- 将结果写入 `slide_window["clean_tool"]`
- 提供基础可视化框和状态文本

### `app/services/inference/workflows/mstcn_runtime.py`

新增 `MSTCNRuntime`。

职责：

- 定义线上可 import 的 MS-TCN++ 网络结构
- 加载 `MS-TCN2/models/Endo_Project/split_1/epoch-50.model`
- 加载 `MS-TCN2/data/Endo_Project/mapping.txt`
- 接收 `[20, T]` 特征序列
- 输出当前阶段、逐帧标签和置信度

### `app/services/inference/workflows/mstcn_features.py`

新增 `window_to_mstcn_features()`。

职责：

- 将 `List[DetectionOutput]` 转为 MS-TCN 输入特征
- 输出 shape 为 `[20, T]`
- 每帧每类保留最高置信度检测
- 每类特征为 `[cx, cy, w, h, conf]`
- 坐标按图像宽高归一化

### `app/services/inference/workflows/mstcn_phase.py`

新增 `MSTCNPhaseAnalyzer`，继承 `TemporalAnalyzer`。

职责：

- 读取 `source_task_name` 指定的滑动窗口
- 将窗口转为 MS-TCN 特征
- 调用 `MSTCNRuntime.predict()`
- 输出 temporal event
- 第一版返回空告警列表：`return events, []`

注意：`MSTCNPhaseAnalyzer` 在 `_sm` 中维护自己的 `frame_buffer` 和 `last_ts`。每次 tick
只把 `timestamp > last_ts` 的新 `DetectionOutput` 追加进自管窗口，再按 `max_frames` 裁剪。
MS-TCN 对这个自管窗口整体做序列判断；`last_window_ts` 只用于避免同一个自管窗口重复推理。

### `app/services/inference/workers/temporal.py`

小改 `ClientTemporalActor`：

```python
source_name = getattr(analyzer, "source_task_name", analyzer.name)
window = self._cq.get_slide_window(source_name)
```

旧 analyzer 没有 `source_task_name` 时仍按原来的 `analyzer.name` 读取窗口。

### `app/services/inference/workflows/__init__.py`

补充导出：

```python
from .mstcn_phase import MSTCNPhaseAnalyzer
```

这样 workflow 包级导出与 YAML 中的 analyzer 类保持一致，也符合 `infer-workflow` skill 中
“装配与注册”部分的检查项。

## 配置变更

`config/inference_config.yaml` 中 `CLEAN` 阶段从 mock 透传改为：

```yaml
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
        model_path: ./MS-TCN2/models/Endo_Project/split_1/epoch-50.model
        mapping_path: ./MS-TCN2/data/Endo_Project/mapping.txt
        feature_dim: 20
        min_frames: 30
        max_frames: 300
        image_width: 640
        image_height: 480
```

`realtime: false` 的含义是：`clean_tool` 不加入 `signals_10s` 告警指标映射。它仍然会运行 detector 和 analyzer，并输出 temporal event。

## 验证方式

### 1. 单元测试

```bash
source .venv/bin/activate
python -m pytest tests/test_mstcn_features.py tests/test_mstcn_phase_chain.py -q
```

验证内容：

- `DetectionOutput` 能正确转为 `[20, T]`
- MS-TCN phase event 能和 bubble event 合并到 `latest_temporal.events`

### 2. 配置加载与实例化

```bash
source .venv/bin/activate
python - <<'PY'
from app.services.inference.config import load_stage_config
from app.services.inference.stage_factory import StageFactory

cfg = load_stage_config()
factory = StageFactory(cfg)
print([d.name for d in factory.create_detectors_for_stage("CLEAN")])
print(factory.create_analyzer_specs_for_stage("CLEAN"))
PY
```

期望能看到：

```text
['clean_tool']
MSTCNPhaseAnalyzer
```

### 3. 启动后端

```bash
bash ./start_backend.sh dev
```

启动日志中应出现：

```text
成功创建 Detector: clean_tool
注册 TemporalAnalyzer spec: clean_tool
```

进入 `CLEAN` 阶段并积累至少 `min_frames=30` 个检测窗口后，前端 temporal events 应出现：

```text
mstcn_phase=... conf=...
```

## 当前边界

- MS-TCN 当前只输出阶段事件，不产生 `AlarmInfo`。
- `LEAK` 阶段原有 `bubble` 和 `bending` 不受影响。
- 若要把阶段识别变成正式流程违规告警，需要在 `MSTCNPhaseAnalyzer` 或后续 `FusionAnalyzer` 中增加业务规则。
- MS-TCN 输入依赖 `CleanToolDetector` 的四类检测结果，类别名必须与训练特征一致。
- 当前链路已经覆盖 skill 要求的 Detector、TemporalAnalyzer、YAML 装配和包级导出；但阶段事件到正式告警/结算结果的业务规则还没有实现。
