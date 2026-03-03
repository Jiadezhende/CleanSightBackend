---
name: infer-workflow
description: "Create a new InferenceWorkflow subclass for CleanSightBackend. Use this skill whenever the user asks to create a new detection task, add a new workflow, implement a new XX detection, or follow the workflow pattern. Trigger on: 新建workflow、新建检测任务、按workflow规范、创建XX检测、add a workflow for、implement detection workflow."
---

# InferenceWorkflow 开发规范

## 1. 目录与文件

```
app/services/inference/workflows/
├── infer_workflow.py   # 基类（不要修改）
├── bubble.py           # 参考：连续帧触发模式
├── bending.py          # 参考：滑动窗口比例触发模式
├── mock.py             # 参考：无模型纯算法示例
└── <your_task>.py      # 新建文件，一个任务一个文件
```

新文件建好后，必须在 `app/services/inference/workflows/__init__.py` 里注册（import + `__all__`）。

---

## 2. 选择基类

| 场景 | 继承 | 说明 |
|------|------|------|
| 使用 YOLO `.pt` 模型 | `YOLOWorkflow` | `infer` / `infer_batch` 已实现，只需传 `model_path` |
| 无模型 / mock / 纯算法 | `InferenceWorkflow` | 需自行实现 `infer` 和 `infer_batch` |

---

## 3. 必须实现的三个方法

### 方法 1 — `infer(frame, context) → DetectionOutput`

**YOLOWorkflow 子类**（直接调用基类推理，捕获异常即可）：

```python
def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
    from app.utils.exceptions import ModelInferenceError
    try:
        return self._run_yolo(frame)
    except RuntimeError as e:
        error_msg = str(e).lower()
        raise ModelInferenceError(
            message=str(e),
            model_name=self.name,
            client_id=context.get("client_id"),
            is_cuda_error="out of memory" in error_msg or "cuda" in error_msg,
        ) from e
```

**InferenceWorkflow 子类**（纯算法，直接返回）：

```python
def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> DetectionOutput:
    # 计算逻辑 ...
    return DetectionOutput(
        detections=[...],
        metadata={"model": "your_algo"},
        timestamp=time.time(),
        success=True,
    )
```

---

### 方法 2 — `analyze_temporal(window, state) → (List[str], List[AlarmInfo])`

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
            alarm_type="流程违规",
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

---

### 方法 3 — `prepare_visualization_data(output) → VisualizationData`

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

---

## 4. 覆盖 `infer_batch`（可选，用于性能优化）

只有 YOLOWorkflow 子类需要此覆盖（利用 YOLO 批量推理接口）：

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

## 5. 数据模型速查

```python
# 单个检测框
Detection(bbox=[x1, y1, x2, y2], confidence=0.9, class_id=0, class_name="bubble")

# 检测输出（infer 返回值）
DetectionOutput(detections=[...], metadata={...}, timestamp=time.time(), success=True)

# 告警（analyze_temporal 在上升沿产出）
AlarmInfo(alarm_type="流程违规", alarm_level="high", alarm_message="...", metadata={...})

# 可视化数据（prepare_visualization_data 返回值）
VisualizationData(type=VisualizationType.BBOX, items=[...],
                  status_text="...", status_color=(B,G,R), status_position="top-left")

# 可视化条目
VisItem(bbox=[x1,y1,x2,y2], label="...", confidence=0.9, color=(B,G,R))
```

---

## 6. 完整新建流程检查清单

- [ ] `app/services/inference/workflows/<name>.py` 已创建
- [ ] 选择了正确的基类（YOLOWorkflow / InferenceWorkflow）
- [ ] `infer()` 已实现
- [ ] `analyze_temporal()` 四步完整（特征 → 计数 → events → 边沿告警）
- [ ] `prepare_visualization_data()` 已实现
- [ ] （可选）`infer_batch()` 已覆盖并含 fallback
- [ ] `__init__.py` 已注册 import 和 `__all__`
