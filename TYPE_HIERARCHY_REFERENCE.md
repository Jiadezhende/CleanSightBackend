# 推理数据类型快速参考

## 架构层次说明

CleanSight 的数据模型分为两个层次：

1. **Task 级别**（data_models.py）- 单个检测任务的数据结构
   - `Detection` - 单个检测对象
   - `DetectionOutput` - 标准化检测输出
   - `TaskInferenceResult` - 单个 Task 的推理结果
   - `TemporalResult` - 单个 Task 的时序分析
   - `VisualizationData` - 单个 Task 的可视化数据
   - `AlarmInfo` - 单个 Task 的告警信息

2. **客户端/Stage 级别**（models.py）- 汇总多个 Task，用于队列通信
   - `InferenceRequest` - 客户端推理请求
   - `InferenceResult` - 汇总多个 Task 的推理结果
   - `TemporalAnalysisResult` - 客户端的时序分析结果
   - `TemporalAnalysisPackage` - 传递给可视化的数据包
   - `WriteBackData` - 最终写回给客户端的数据

## 类型层次结构

```
[客户端级别 - models.py]
InferenceResult                              # 队列传递单位，汇总多个 Task
│
├── client_id: str
├── timestamp: float
├── stage: str
├── frame: np.ndarray                        # 原始帧
│
└── result: Dict[str, TaskInferenceResult]   # 各 Task 的结果（多个 Task）
          │
          │ [Task 级别 - data_models.py]
          │
          └── TaskInferenceResult            # 单个 Task 的推理结果
              │
              ├── detection_output: DetectionOutput  # 标准化检测输出
              │   │
              │   ├── detections: List[Detection]    # 检测对象列表
              │   │   └── Detection
              │   │       ├── bbox: List[int]
              │   │       ├── confidence: float
              │   │       ├── class_id: int
              │   │       ├── class_name: str
              │   │       ├── mask: Optional[np.ndarray]
              │   │       └── keypoints: Optional[List]
              │   │
              │   ├── metadata: Dict[str, Any]       # Task 元数据
              │   └── timestamp: float
              │
              ├── success: bool                      # 推理是否成功
              ├── error: str (可选)                 # 错误信息
              │
              └── 向后兼容字段 (可选)
                  ├── bubble_detected: bool
                  ├── bubble_count: int
                  ├── bending_detected: bool
                  └── detection_count: int

[客户端级别 - models.py]
TemporalAnalysisResult                       # 客户端的时序分析结果（跨 Task）
│
├── client_id: str
├── timestamp: float
├── stage_changed: bool                      # 客户端是否切换 stage
├── new_stage: Optional[str]
├── step_completed: bool                     # 客户端当前步骤是否完成
├── events: List[str]                        # 所有 Task 触发的事件
└── state_snapshot: Dict[str, Any]           # 客户端状态快照
```

## 核心类型定义

### 客户端/Stage 级别（models.py）

#### 1. InferenceResult

**用途**：队列传递的推理结果，汇总一个客户端在某个 stage 的所有 Task 结果

**层次**：客户端级别（包含多个 Task 的结果）

```python
@dataclass
class InferenceResult:
    client_id: str
    timestamp: float
    stage: str
    result: Dict[str, TaskInferenceResult]  # ✅ 类型清晰！
    annotated_frame: Optional[np.ndarray] = None
    frame: Optional[np.ndarray] = None
```

**示例**：
```python
InferenceResult(
    client_id="client_001",
    timestamp=1234567890.123,
    stage="LEAK",
    result={
        "bubble_detection": {
            "detection_output": DetectionOutput(...),
            "success": True,
            "bubble_detected": True,
            "bubble_count": 5
        },
        "bending_detection": {
            "detection_output": DetectionOutput(...),
            "success": True,
            "bending_detected": False
        }
    },
    frame=frame_array
)
```

#### 2. TemporalAnalysisResult

**用途**：客户端的时序分析结果（跨多个 Task）

**层次**：客户端级别（汇总所有 Task 的时序事件）

```python
@dataclass
class TemporalAnalysisResult:
    client_id: str
    timestamp: float
    stage_changed: bool           # 客户端是否切换 stage
    new_stage: Optional[str]
    step_completed: bool          # 客户端当前步骤是否完成
    events: List[str]             # 所有 Task 触发的事件
    state_snapshot: Dict[str, Any]  # 客户端状态快照
```

### Task 级别（data_models.py）

#### 3. TaskInferenceResult

**用途**：单个 Task 的推理结果格式

**层次**：Task 级别（单个检测任务的输出）

```python
class TaskInferenceResult(TypedDict, total=False):
    detection_output: DetectionOutput  # 必需
    success: bool                      # 必需
    error: str                        # 可选
    # ...其他向后兼容字段
```

**示例**：
```python
{
    "detection_output": DetectionOutput(
        detections=[
            Detection(
                bbox=[100, 200, 300, 400],
                confidence=0.95,
                class_id=0,
                class_name="bubble"
            )
        ],
        metadata={"model": "yolov8", "inference_time_ms": 15.2},
        timestamp=1234567890.123
    ),
    "success": True,
    "bubble_detected": True,  # 向后兼容字段
    "bubble_count": 1
}
```

#### 4. DetectionOutput

**用途**：标准化检测输出

**层次**：Task 级别（单个检测任务的输出格式）

```python
@dataclass
class DetectionOutput:
    detections: List[Detection]
    metadata: Dict[str, Any]
    timestamp: float
```

#### 5. Detection

**用途**：单个检测对象

**层次**：Task 级别（检测输出的最小单位）

```python
@dataclass
class Detection:
    bbox: List[int]         # [x1, y1, x2, y2]
    confidence: float       # [0.0-1.0]
    class_id: int
    class_name: str
    mask: Optional[np.ndarray] = None
    keypoints: Optional[List] = None
    extra: Dict[str, Any] = field(default_factory=dict)
```

#### 6. TemporalResult

**用途**：单个 Task 的时序分析结果

**层次**：Task 级别（单个检测任务的时序状态）

```python
@dataclass
class TemporalResult:
    detected: bool                  # 当前帧是否检测到目标
    event_triggered: bool           # 是否触发时序事件
    event_message: Optional[str]    # 事件描述
    counters: Dict[str, Any]        # 计数器
    metadata: Dict[str, Any]        # 元数据
```

#### 7. VisualizationData

**用途**：单个 Task 的可视化数据

**层次**：Task 级别（单个检测任务的可视化要求）

```python
@dataclass
class VisualizationData:
    type: str                       # "bbox", "mask", "heatmap", "keypoint"
    items: List[VisItem]            # 可视化项列表
    status_text: str                # 状态栏文本
    status_color: Tuple[int, int, int]  # 状态栏颜色
```

## 数据流示例

### 完整数据流（展示层次关系）

```python
# ===== Task 级别 =====

# 1. Task 推理（单个 Task）
frames = [frame1, frame2, frame3]
task = BubbleDetectionTask("bubble", "model.pt")

# Task.infer_batch() 返回: List[TaskInferenceResult]
task_results = task.infer_batch(frames, contexts)
# task_results = [
#     {
#         "detection_output": DetectionOutput(...),  # Task 级别
#         "success": True,
#         "bubble_detected": True
#     },
#     ...
# ]

# ===== 客户端级别 =====

# 2. MultiModelWorkerPool 汇总多个 Task: List[InferenceResult]
inference_results = [
    InferenceResult(
        client_id="client_001",    # 客户端级别
        timestamp=frame1.timestamp,
        stage="LEAK",              # Stage 级别
        result={
            # 包含多个 Task 的结果
            "bubble_detection": task_results[0],      # Task 级别
            "bending_detection": other_task_result    # Task 级别
        },
        frame=frame1
    ),
    ...
]

# 3. 投递到时序队列（客户端级别）
temporal_queue.put(inference_results[0])

# 4. TemporalWorker 处理（客户端级别 → Task 级别）
result: InferenceResult = temporal_queue.get()  # 客户端级别

# ✅ 类型清晰，IDE 可以自动补全
for task_name, task_result in result.result.items():
    # task_result: TaskInferenceResult (Task 级别)
    detection_output: DetectionOutput = task_result["detection_output"]
    
    if task_result["success"]:
        # 访问检测结果（Task 级别）
        for det in detection_output.detections:
            print(f"{det.class_name}: {det.confidence:.2f}")
        
        # Task 级别：时序分析
        temporal: TemporalResult = task.analyze_temporal(
            state, detection_output, result.timestamp
        )

# 5. 汇总客户端级别的时序分析结果
temporal_analysis = TemporalAnalysisResult(
    client_id="client_001",
    timestamp=result.timestamp,
    stage_changed=False,
    new_stage=None,
    step_completed=False,
    events=["bubble_detected", "bending_not_detected"],  # 汇总所有 Task 的事件
    state_snapshot={...}
)
```

## 类型访问模式

### 模式 1：从客户端级别访问 Task 级别数据

```python
def process_inference_result(result: InferenceResult):
    # result: 客户端级别（汇总多个 Task）
    
    # 遍历所有 Task
    for task_name, task_result in result.result.items():
        # task_result: TaskInferenceResult (Task 级别)
        
        if not task_result["success"]:
            logger.error(f"Task {task_name} failed: {task_result.get('error')}")
            continue
        
        # 访问 Task 级别的标准化检测输出
        detection_output: DetectionOutput = task_result["detection_output"]
        
        # 访问 Task 级别的检测对象
        for det in detection_output.detections:
            print(f"{det.class_name} @ {det.bbox} ({det.confidence:.2f})")
```

### 模式 2：Task 级别实现

```python
class MyDetectionTask(InferenceTask):
    def infer_batch(self, frames, contexts) -> List[Dict[str, Any]]:
        # 返回 Task 级别的结果: List[TaskInferenceResult]
        return [
            {
                "detection_output": self.adapter.adapt(raw, frame, time.time()),
                "success": True,
                "my_custom_field": value  # 向后兼容字段
            }
            for raw, frame in zip(raw_outputs, frames)
        ]
    
    def analyze_temporal(self, state, detection_output, timestamp) -> TemporalResult:
        # 返回 Task 级别的时序分析结果
        return TemporalResult(
            detected=len(detection_output.detections) > 0,
            event_triggered=False,
            event_message=None,
            counters={"detection_count": len(detection_output.detections)}
        )
```

### 模式 3：TemporalWorker 处理（客户端级别 → Task 级别）

```python
def _process_with_tasks(self, result: InferenceResult, state):
    # result: 客户端级别（包含多个 Task）
    tasks = self.stage_configs[result.stage]["models"]
    
    all_events = []  # 收集所有 Task 的事件（汇总到客户端级别）
    
    for task in tasks:
        task_result = result.result.get(task.name, {})
        
        # 检查是否有 detection_output (Task 级别)
        if "detection_output" not in task_result:
            continue
        
        # Task 级别：获取检测输出
        detection_output: DetectionOutput = task_result["detection_output"]
        
        # Task 级别：时序分析
        temporal: TemporalResult = task.analyze_temporal(
            state, detection_output, result.timestamp
        )
        
        # Task 级别：告警评估
        alarms: List[AlarmInfo] = task.evaluate_alarms(temporal, context)
        
        # 收集 Task 级别的事件
        if temporal.event_triggered:
            all_events.append(temporal.event_message)
    
    # 客户端级别：汇总所有 Task 的结果
    return TemporalAnalysisResult(
        client_id=result.client_id,
        timestamp=result.timestamp,
        stage_changed=check_stage_change(state),
        events=all_events,  # 汇总所有 Task 的事件
        state_snapshot=state.to_dict()
    )
```

## 类型检查

### 使用 mypy 验证

```bash
# 检查类型注解
mypy app/services/inference/workers/temporal.py
mypy app/services/inference/workers/base.py
```

### IDE 自动补全

在支持类型提示的 IDE（如 VSCode + Pylance）中：

```python
result: InferenceResult = queue.get()

# ✅ IDE 会自动提示：
#   result.client_id
#   result.timestamp
#   result.stage
#   result.result  # Dict[str, TaskInferenceResult]

task_result = result.result["bubble_detection"]

# ✅ IDE 会自动提示：
#   task_result["detection_output"]  # DetectionOutput
#   task_result["success"]            # bool
#   task_result["error"]              # str (可选)

detection_output = task_result["detection_output"]

# ✅ IDE 会自动提示：
#   detection_output.detections  # List[Detection]
#   detection_output.metadata    # Dict[str, Any]
#   detection_output.timestamp   # float

for det in detection_output.detections:
    # ✅ IDE 会自动提示：
    #   det.bbox
    #   det.confidence
    #   det.class_name
    #   ...
```

## 常见问题

### Q1: 为什么 InferenceResult.result 不直接使用 List[DetectionOutput]？

**A**: 因为：
1. 一个 stage 可能有多个 Task（如 bubble + bending）
2. 需要保存 Task 名称和向后兼容字段
3. 需要标记 success/error 状态

### Q2: TaskInferenceResult 为什么是 TypedDict 而不是 @dataclass？

**A**: 因为：
1. 向后兼容：允许添加自定义字段（如 bubble_detected）
2. 灵活性：不同 Task 可以添加不同的字段
3. 逐步迁移：旧代码仍能使用字典访问方式

### Q3: 什么时候用 models.py，什么时候用 data_models.py？

**A**:
- **data_models.py** (Task 级别): 单个检测任务的数据结构
  - `DetectionOutput` - 单个 Task 的检测输出
  - `TemporalResult` - 单个 Task 的时序分析
  - `VisualizationData` - 单个 Task 的可视化数据
  - `AlarmInfo` - 单个 Task 的告警信息
  
- **models.py** (客户端/Stage 级别): 汇总多个 Task 的结果，用于队列通信
  - `InferenceResult` - 汇总多个 Task 的推理结果（result: Dict[str, TaskInferenceResult]）
  - `TemporalAnalysisResult` - 客户端的时序分析结果（跨 Task）
  - `TemporalAnalysisPackage` - 传递给可视化的数据包
  - `WriteBackData` - 最终写回数据

**关系**: models.py 是客户端级别的包装器，内部包含多个 Task 级别的 data_models.py 类型。

### Q4: TemporalResult 和 TemporalAnalysisResult 有什么区别？

**A**:
- **TemporalResult** (data_models.py, Task 级别): 
  - 单个 Task 的时序分析结果
  - 例如：BubbleDetectionTask 的连续帧检测状态
  
- **TemporalAnalysisResult** (models.py, 客户端级别):
  - 整个客户端的时序分析结果（跨多个 Task）
  - 包含：stage 切换、步骤完成、所有 Task 的事件汇总

## 迁移建议

### 当前状态
- ✅ 类型定义已添加（Task 级别 + 客户端级别）
- ✅ 文档已更新，层次关系明确
- ✅ 向后兼容

### 后续优化
1. **短期**（1个月）：更新所有 Task 的类型注解，区分 Task 级别和客户端级别
2. **中期**（3个月）：启用 mypy 类型检查，强制层次约束
3. **长期**（6个月）：移除向后兼容字段，统一使用 detection_output

### 层次清晰的代码风格

**推荐**：明确注释层次关系
```python
# 客户端级别：获取推理结果
result: InferenceResult = queue.get()

# Task 级别：遍历各个 Task
for task_name, task_result in result.result.items():
    # Task 级别：获取检测输出
    detection_output: DetectionOutput = task_result["detection_output"]
    
    # Task 级别：时序分析
    temporal: TemporalResult = task.analyze_temporal(...)

# 客户端级别：汇总时序分析
client_temporal: TemporalAnalysisResult = aggregate_temporal_results(...)
```

---

**最后更新**: 2024-12-XX  
**相关文档**: 
- [TASK_CENTRIC_ARCHITECTURE.md](TASK_CENTRIC_ARCHITECTURE.md)
- [TYPE_OPTIMIZATION_PROPOSAL.md](TYPE_OPTIMIZATION_PROPOSAL.md)

**关键概念**：
- **Task 级别** (data_models.py): 单个检测任务的数据结构
- **客户端/Stage 级别** (models.py): 汇总多个 Task 的结果，用于队列通信
