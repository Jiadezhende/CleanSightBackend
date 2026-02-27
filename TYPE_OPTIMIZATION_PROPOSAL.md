# 类型定义优化方案

## 问题诊断

当前存在两套数据模型混用的情况：

### 1. 旧模型（models.py）- 用于队列传递
```python
@dataclass
class InferenceResult:
    result: Dict[str, Any]  # ❌ 类型不清晰
```

### 2. 新模型（data_models.py）- 强类型标准
```python
@dataclass
class DetectionOutput:  # ✅ 强类型
    detections: List[Detection]
    metadata: Dict[str, Any]
    timestamp: float
```

### 3. 实际运行时结构
```python
# InferenceResult.result 的实际内容：
{
    "task_name": {
        "detection_output": DetectionOutput,  # 新架构的强类型
        "bubble_detected": bool,              # 向后兼容字段
        "success": bool
    }
}
```

## 推荐方案：创建 TaskInferenceResult 类型

### 方案 A：定义中间类型（推荐）

在 `data_models.py` 中添加：

```python
from typing import TypedDict

class TaskInferenceResult(TypedDict):
    """单个 Task 的推理结果（标准格式）
    
    这是 InferenceResult.result[task_name] 的类型定义
    """
    detection_output: DetectionOutput  # 标准化检测输出（必需）
    success: bool                      # 推理是否成功（必需）
    error: NotRequired[str]           # 错误信息（可选，失败时提供）
    
    # 向后兼容字段（可选，用于旧代码）
    # Task 可以添加自定义字段，但建议逐步迁移到 detection_output
```

在 `models.py` 中更新类型注解：

```python
from typing import Dict
from app.services.inference.data_models import TaskInferenceResult

@dataclass
class InferenceResult:
    """推理结果：关联客户端
    
    Attributes:
        result: 各 Task 的推理结果
                格式: {task_name: TaskInferenceResult}
                
                示例:
                {
                    "bubble_detection": {
                        "detection_output": DetectionOutput(...),
                        "success": True,
                        "bubble_detected": True,  # 向后兼容
                        "bubble_count": 5
                    }
                }
    """
    client_id: str
    timestamp: float
    stage: str
    result: Dict[str, TaskInferenceResult]  # ✅ 类型更清晰！
    annotated_frame: Optional[np.ndarray] = None
    frame: Optional[np.ndarray] = None
```

### 方案 B：使用泛型（更灵活）

```python
from typing import TypeVar, Generic, Protocol

class TaskResult(Protocol):
    """Task 推理结果协议"""
    detection_output: DetectionOutput
    success: bool

@dataclass
class InferenceResult(Generic[TaskResult]):
    result: Dict[str, TaskResult]
```

## 迁移路径

### 阶段 1：添加类型定义（不破坏现有代码）
- ✅ 在 data_models.py 添加 TaskInferenceResult
- ✅ 更新 InferenceResult 的类型注解
- ✅ 现有代码继续工作（Python 的类型注解是可选的）

### 阶段 2：更新 Task 实现
```python
class BubbleDetectionTask(InferenceTask):
    def infer_batch(self, frames, contexts) -> List[TaskInferenceResult]:
        # 返回强类型结果
        return [
            {
                "detection_output": output,
                "success": True,
                "bubble_detected": len(output.detections) > 0,  # 兼容字段
                "bubble_count": len(output.detections)
            }
            for output in detection_outputs
        ]
```

### 阶段 3：移除向后兼容字段
- 逐步移除 `bubble_detected` 等冗余字段
- 统一使用 `detection_output.detections`

## 回答用户的问题

### Q1: 当前实现中推理结果类型到底是什么？

**A1**: 
- 队列传递：`InferenceResult` (定义在 models.py)
- 字段类型：`result: Dict[str, Any]` (类型不清晰)
- 实际内容：`{task_name: {"detection_output": DetectionOutput, ...}}`

### Q2: 如何优化类型定义增强可读性？

**A2**: 
1. 创建 `TaskInferenceResult` TypedDict 定义 Task 返回值结构
2. 更新 `InferenceResult.result` 类型为 `Dict[str, TaskInferenceResult]`
3. 使用类型检查工具（mypy）验证

### Q3: models.py 和 data_models.py 以哪个为准？

**A3**:
- **data_models.py 为准**（新架构的标准数据模型）
- **models.py 用于队列通信**（保持向后兼容，但内部应使用 data_models）

**关系**：
```
data_models.py (标准)
    ↓ 被包含在
models.py (通信层)
    ↓ 流经
Worker Pipeline (temporal → visualization → writeback)
```

## 最佳实践

### 推荐的类型层次

```
InferenceResult                    # 队列传递单位
  ├── result: Dict[str, TaskInferenceResult]
  │     └── TaskInferenceResult    # Task 返回格式
  │           ├── detection_output: DetectionOutput  # 核心数据
  │           └── success: bool
  │
  ├── frame: np.ndarray           # 原始帧
  └── timestamp: float

TemporalAnalysisPackage            # 时序分析后
  ├── inference_result            # 同上
  ├── temporal_result: TemporalAnalysisResult
  └── frontend_message: FrontendMessage

WriteBackData                      # 最终形态
  ├── processed_frame             # 可视化后
  ├── inference_result
  └── frontend_message
```

### 代码示例

```python
# 1. Task 实现（强类型返回）
def infer_batch(self, frames, contexts) -> List[TaskInferenceResult]:
    return [
        {
            "detection_output": self.adapter.adapt(raw, frame, time.time()),
            "success": True
        }
        for raw, frame in zip(raw_outputs, frames)
    ]

# 2. TemporalWorker 使用（类型清晰）
result: InferenceResult = self.input_queue.get()
for task_name, task_result in result.result.items():
    # ✅ IDE 可以自动补全
    detection_output: DetectionOutput = task_result["detection_output"]
    if task_result["success"]:
        temporal = task.analyze_temporal(state, detection_output, timestamp)
```

## 总结

1. **当前问题**：`Dict[str, Any]` 类型太泛化，无法体现实际结构
2. **根本原因**：新旧数据模型混用，缺少中间层类型定义
3. **解决方案**：创建 `TaskInferenceResult` 明确 Task 返回值结构
4. **迁移策略**：渐进式，先添加类型注解，不破坏现有代码
5. **长期目标**：`data_models.py` 为标准，`models.py` 只做通信包装

---

**实施优先级**：
- 🔴 **高**：添加 TaskInferenceResult TypedDict（立即提升可读性）
- 🟡 **中**：更新所有 Task 的类型注解
- 🟢 **低**：移除向后兼容字段（3-6 个月后）
