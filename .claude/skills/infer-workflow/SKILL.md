---
name: infer-workflow
description: "Create a new InferenceWorkflow subclass for CleanSightBackend. Use this skill whenever the user asks to create a new detection task, add a new workflow, implement a new XX detection, create a new model-type base workflow, or follow the workflow pattern. Trigger on: 新建workflow、新建检测任务、按workflow规范、创建XX检测、add a workflow for、implement detection workflow、新建模型基类."
---

# InferenceWorkflow 开发规范

## 1. 架构总览

项目采用三级继承体系来组织推理工作流：

```
InferenceWorkflow (抽象基类 — 定义推理、时序分析、可视化的统一接口)
├── YOLOWorkflow (YOLO 模型中间基类 — 封装模型加载/推理/输出适配)
│   ├── BubbleDetectionTask        (气泡检测：连续帧触发)
│   └── EndoscopeBendingDetectionTask (弯折检测：滑动窗口比例触发)
├── MockDetectionTask (纯算法示例 — 无模型，直接继承 InferenceWorkflow)
└── [未来] 其他模型中间基类（分类、分割等）
```

**设计理念**：`InferenceWorkflow` 定义所有推理流程的通用契约；针对特定模型类型（如 YOLO），衍生出中间基类（如 `YOLOWorkflow`）封装该类模型的共性逻辑（模型加载、推理调用、输出适配），让具体检测任务只关注业务逻辑（时序判定、告警策略、可视化样式）。后续如需对接分类模型、分割模型等，可参照 `YOLOWorkflow` 的模式新建其他中间基类。

---

## 2. 目录与文件

```
app/services/inference/workflows/
├── infer_workflow.py   # 基类 InferenceWorkflow + YOLOWorkflow（不要修改）
├── bubble.py           # 参考：连续帧触发模式（继承 YOLOWorkflow）
├── bending.py          # 参考：滑动窗口比例触发模式（继承 YOLOWorkflow）
├── mock.py             # 参考：无模型纯算法示例（继承 InferenceWorkflow）
└── <your_task>.py      # 新建文件，一个任务一个文件
```

新文件建好后，必须在 `app/services/inference/workflows/__init__.py` 里注册（import + `__all__`）。

---

## 3. 确定继承层级

根据任务需求，选择正确的继承起点：

| 你要做什么 | 继承谁 | 需要实现什么 |
|-----------|--------|------------|
| **新增一个 YOLO 检测任务**（最常见） | `YOLOWorkflow` | `analyze_temporal` + `prepare_visualization_data`，`infer` 和 `infer_batch` 已由 YOLOWorkflow 提供 |
| **无模型 / 纯算法检测** | `InferenceWorkflow` | `infer` + `analyze_temporal` + `prepare_visualization_data` |
| **新增一种模型类型的中间基类**（如分类模型、分割模型） | `InferenceWorkflow` | `infer` + `infer_batch` 等通用推理逻辑，把 `analyze_temporal` 和 `prepare_visualization_data` 留给下游具体任务 |

> **提示**：绝大多数场景是新增一个基于 YOLO 模型的检测任务，此时继承 `YOLOWorkflow` 即可——它已经封装了模型加载、单帧/批量推理、输出适配，你只需关注时序分析和可视化。

---

## 4. 新增 YOLO 检测任务（继承 YOLOWorkflow）

这是最常见的场景。只需实现两个方法：`analyze_temporal` 和 `prepare_visualization_data`。

### 4.1 构造函数

```python
class MyDetectionTask(YOLOWorkflow):
    """我的检测任务"""

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.5,
        iou_threshold: float = 0.45,
        enabled: bool = True,
    ):
        super().__init__(
            name="my_detection",
            model_path=model_path,
            conf_threshold=conf_threshold,
            iou_threshold=iou_threshold,
            enabled=enabled,
        )
        self.consecutive_trigger = 3  # 或其他业务参数
```

### 4.2 analyze_temporal — 时序分析

**必须严格按以下四步顺序实现，不可省略任何一步：**

```python
def analyze_temporal(
    self, window: List[DetectionOutput], state: ClientState,
) -> Tuple[List[str], List[AlarmInfo]]:
    if not window:
        return [], []

    # ① 计算时序特征（只算一次，不要在后面重复遍历 window）
    #    模式 A：连续帧计数（参考 bubble.py）
    consecutive = 0
    for output in reversed(window):
        if len(output.detections) > 0:
            consecutive += 1
        else:
            break
    is_triggered = consecutive >= self.consecutive_trigger

    #    模式 B：滑动窗口比例（参考 bending.py）
    # latest_ts = window[-1].timestamp
    # recent = [o for o in window if o.timestamp >= latest_ts - self.window_seconds]
    # ratio = sum(1 for o in recent if len(o.detections) > 0) / len(recent)
    # is_triggered = ratio >= self.trigger_ratio

    # ② 更新累计计数器
    latest = window[-1]
    if len(latest.detections) > 0:
        state.increment_counter("xxx_total", delta=len(latest.detections))

    # ③ 生成 events（前端展示，不涉及告警逻辑）
    events = ["连续N帧检测到xxx"] if is_triggered else []

    # ④ 边沿触发告警（rising edge 发出，falling edge 复位）
    alarms: List[AlarmInfo] = []
    was_alarming = state.get_counter("xxx_alarming", 0) > 0

    if is_triggered and not was_alarming:          # 0→1 上升沿
        state.increment_counter("xxx_alarming")
        state.increment_counter("xxx_alarm_count")
        alarms.append(AlarmInfo(
            alarm_type=AlarmType.PROCESS_VIOLATION,
            alarm_level="high",                    # low / medium / high / critical
            alarm_message="检测到 xxx 异常",
            metadata={"consecutive_frames": consecutive},
        ))
    elif not is_triggered and was_alarming:        # 1→0 下降沿，复位
        state.reset_counter("xxx_alarming")

    return events, alarms
```

> **为什么要边沿触发而非电平触发？**
> 电平触发在持续异常时会不断重复投递告警，淹没下游；边沿触发只在状态变化时发出一次，更符合告警语义。

### 4.3 prepare_visualization_data — 可视化

```python
def prepare_visualization_data(self, output: DetectionOutput) -> VisualizationData:
    items = []
    for det in output.detections:
        items.append(VisItem(
            bbox=det.bbox,
            label=f"{det.class_name} {det.confidence:.2f}",
            confidence=det.confidence,
            color=(B, G, R),   # OpenCV BGR 元组，如红色 = (0, 0, 255)
        ))

    detected = len(output.detections) > 0
    return VisualizationData(
        type=VisualizationType.BBOX,
        items=items,
        status_text="Detected!" if detected else "Normal",
        status_color=(0, 0, 255) if detected else (0, 255, 0),
        status_position="top-left",   # top-left / top-right / bottom-left / bottom-right
    )
```

### 4.4 覆盖 infer_batch（可选，用于性能优化）

YOLOWorkflow 已提供默认的批量推理实现。如需添加业务字段，可覆盖：

```python
def infer_batch(
    self, frames: List[np.ndarray], contexts: List[Dict[str, Any]]
) -> List[DetectionOutput]:
    try:
        outputs = self._run_yolo_batch(frames)
        for output in outputs:
            output.success = True
            output.xxx_detected = len(output.detections) > 0   # 业务字段
        return outputs
    except Exception as e:
        logger.error(f"[{self.name}] Batch failed, fallback: {e}", exc_info=True)
        # fallback：逐帧调用
        results = []
        for f, c in zip(frames, contexts):
            try:
                out = self.infer(f, c)
                out.xxx_detected = len(out.detections) > 0
                results.append(out)
            except Exception as err:
                results.append(DetectionOutput(
                    detections=[], metadata={"error": str(err)},
                    timestamp=time.time(), success=False, error=str(err),
                ))
        return results
```

---

## 5. 新增纯算法检测任务（继承 InferenceWorkflow）

当不需要模型时（mock、纯数学算法等），直接继承 `InferenceWorkflow`，需要额外实现 `infer` 方法：

```python
class MyAlgoTask(InferenceWorkflow):
    def __init__(self, enabled: bool = True):
        super().__init__(name="my_algo", enabled=enabled)

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        # 计算逻辑 ...
        return DetectionOutput(
            detections=[...],
            metadata={"model": "your_algo"},
            timestamp=time.time(),
            success=True,
        )

    # analyze_temporal 和 prepare_visualization_data 同第 4 节
```

参考 `mock.py` 获取完整示例。

---

## 6. 新增模型类型中间基类（继承 InferenceWorkflow）

当需要对接一种全新的模型类型（如分类模型、分割模型），且预期会有多个具体检测任务复用同一类模型时，可参照 `YOLOWorkflow` 的模式创建新的中间基类。

**设计要点**：
- 继承 `InferenceWorkflow`
- 封装该类模型的加载、推理调用、输出适配等共性逻辑
- 实现 `infer` 和 `infer_batch`，让下游具体任务无需关心模型调用细节
- 把 `analyze_temporal` 和 `prepare_visualization_data` 保留为抽象，由具体任务实现

```python
class ClassificationWorkflow(InferenceWorkflow):
    """分类模型工作流基类（示例）"""

    def __init__(self, name: str, model_path: str, enabled: bool = True):
        super().__init__(name=name, enabled=enabled)
        self.model_path = model_path
        self._model = None

    def _ensure_model_loaded(self):
        if self._model is None:
            # 加载分类模型 ...
            pass

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
        self._ensure_model_loaded()
        # 分类推理 → 适配为 DetectionOutput ...
        pass

    # analyze_temporal / prepare_visualization_data 留给子类
```

---

## 7. 数据模型速查

```python
# 单个检测框
Detection(bbox=[x1, y1, x2, y2], confidence=0.9, class_id=0, class_name="bubble")

# 检测输出（infer 返回值）
DetectionOutput(detections=[...], metadata={...}, timestamp=time.time(), success=True)

# 告警（analyze_temporal 在上升沿产出）
AlarmInfo(alarm_type=AlarmType.PROCESS_VIOLATION, alarm_level="high", alarm_message="...", metadata={...})

# 可视化数据（prepare_visualization_data 返回值）
VisualizationData(type=VisualizationType.BBOX, items=[...],
                  status_text="...", status_color=(B,G,R), status_position="top-left")

# 可视化条目
VisItem(bbox=[x1,y1,x2,y2], label="...", confidence=0.9, color=(B,G,R))
```

---

## 8. 完整新建流程检查清单

### 新增 YOLO 检测任务（最常见）
- [ ] `app/services/inference/workflows/<name>.py` 已创建
- [ ] 继承 `YOLOWorkflow`，构造函数传入 `model_path` 等参数
- [ ] `analyze_temporal()` 四步完整（特征 → 计数 → events → 边沿告警）
- [ ] `prepare_visualization_data()` 已实现
- [ ] （可选）`infer_batch()` 已覆盖并含 fallback
- [ ] `__init__.py` 已注册 import 和 `__all__`

### 新增纯算法检测任务
- [ ] `app/services/inference/workflows/<name>.py` 已创建
- [ ] 继承 `InferenceWorkflow`
- [ ] `infer()` 已实现
- [ ] `analyze_temporal()` 四步完整
- [ ] `prepare_visualization_data()` 已实现
- [ ] `__init__.py` 已注册 import 和 `__all__`

### 新增模型类型中间基类
- [ ] 在 `infer_workflow.py` 或独立文件中创建新类
- [ ] 继承 `InferenceWorkflow`
- [ ] 封装模型加载、推理调用、输出适配
- [ ] 实现 `infer` 和 `infer_batch`
- [ ] `analyze_temporal` 和 `prepare_visualization_data` 保留为抽象
