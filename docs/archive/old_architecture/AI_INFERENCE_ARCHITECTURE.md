# AI 推理架构设计文档

## 概述

本系统采用可扩展的推理任务架构，支持多种AI模型并行或串行执行。每个推理任务都是独立的模块，可以方便地添加、移除或禁用。

## 核心概念

### 1. InferenceTask（推理任务）

所有推理任务的基类，定义了统一的接口：

```python
class InferenceTask(ABC):
    def infer(self, frame, context) -> InferenceResult:
        """执行推理"""
        pass
    
    def visualize(self, frame, result) -> np.ndarray:
        """可视化结果"""
        pass
    
    def requires_context(self) -> List[str]:
        """声明依赖的其他任务"""
        return []
```

### 2. TaskRegistry（任务注册表）

管理所有推理任务，自动处理任务执行顺序：

- 无依赖的任务并行执行
- 有依赖的任务按依赖顺序串行执行

### 3. 执行流程

```
输入帧
  ↓
[并行执行独立任务]
  ├─ DetectionTask
  ├─ BubbleDetectionTask  
  └─ ...
  ↓
[串行执行依赖任务]
  ├─ MotionTask (依赖 DetectionTask)
  ├─ CleanlinessTask (依赖 DetectionTask + BubbleDetectionTask)
  └─ ...
  ↓
[合并可视化结果]
  ↓
输出帧
```

## 内置任务

### DetectionTask
- **功能**: 关键点检测
- **依赖**: 无
- **输出**: 处理后的帧 + 关键点信息

### MotionTask
- **功能**: 动作分析（弯曲、浸泡等）
- **依赖**: DetectionTask
- **输出**: 动作分析结果

## 如何添加新的推理任务

### 步骤 1: 创建任务类

```python
from app.services.ai import InferenceTask, InferenceResult
import numpy as np
from typing import Dict, Any

class MyCustomTask(InferenceTask):
    def __init__(self):
        super().__init__(name="my_custom_task", enabled=True)
    
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """实现推理逻辑"""
        try:
            # 1. 如果需要其他任务的结果
            other_result = context.get("results", {}).get("detection", {})
            
            # 2. 执行你的推理逻辑
            result = self._my_inference_logic(frame)
            
            # 3. 返回结果
            return {
                "success": True,
                "my_data": result
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """实现可视化逻辑"""
        if not result.get("success"):
            return frame
        
        result_frame = frame.copy()
        # 在 result_frame 上绘制你的结果
        # cv2.putText(result_frame, ...)
        return result_frame
    
    def requires_context(self) -> List[str]:
        """如果依赖其他任务，返回任务名称列表"""
        return ["detection"]  # 或返回 [] 表示无依赖
    
    def _my_inference_logic(self, frame):
        """你的推理实现"""
        pass
```

### 步骤 2: 注册任务

有两种方式注册任务：

#### 方法 A: 在启动时注册（推荐）

编辑 `app/services/ai.py`，在 `_register_default_tasks` 方法中添加：

```python
def _register_default_tasks(self):
    self._task_registry.register(DetectionTask())
    self._task_registry.register(MotionTask())
    self._task_registry.register(MyCustomTask())  # 添加你的任务
```

#### 方法 B: 动态注册

```python
from app.services.ai import manager
from my_module import MyCustomTask

# 注册新任务
manager.register_task(MyCustomTask())

# 启动服务
manager.start()
```

### 步骤 3: 启用/禁用任务

```python
# 禁用某个任务
manager.enable_task("my_custom_task", enabled=False)

# 重新启用
manager.enable_task("my_custom_task", enabled=True)
```

## 示例：气泡检测任务

参见 `app/services/example_custom_task.py` 中的完整示例：

```python
class BubbleDetectionTask(InferenceTask):
    """独立的气泡检测任务"""
    
    def __init__(self):
        super().__init__(name="bubble_detection", enabled=True)
    
    def infer(self, frame, context):
        # 使用 HoughCircles 检测圆形气泡
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        circles = cv2.HoughCircles(gray, ...)
        
        return {
            "success": True,
            "bubble_count": len(circles),
            "bubbles": circles
        }
    
    def visualize(self, frame, result):
        # 绘制检测到的气泡
        for bubble in result["bubbles"]:
            cv2.circle(frame, bubble["center"], bubble["radius"], ...)
        return frame
```

## 性能优化

### 并行执行

系统使用 `ThreadPoolExecutor` 并行执行独立任务：

```python
# 这些任务会并行执行
DetectionTask()        # 无依赖
BubbleDetectionTask()  # 无依赖
StainDetectionTask()   # 无依赖
```

### 依赖管理

有依赖的任务会在依赖完成后执行：

```python
# MotionTask 会等待 DetectionTask 完成
class MotionTask(InferenceTask):
    def requires_context(self):
        return ["detection"]  # 依赖 detection
```

### 任务超时

每个并行任务有 5 秒超时限制，超时会被标记为失败。

## 上下文（Context）结构

推理任务可以访问上下文信息：

```python
context = {
    "task": CleaningTask对象,  # 当前清洗任务
    "results": {               # 其他任务的结果
        "detection": {
            "success": True,
            "keypoints": {...},
            "processed_frame": np.ndarray
        },
        "bubble_detection": {
            "success": True,
            "bubble_count": 3,
            "bubbles": [...]
        }
    }
}
```

## 结果结构

每个任务返回标准化的结果：

```python
{
    "success": True/False,     # 是否成功
    "error": "错误信息",        # 失败时的错误
    # ... 其他自定义字段
}
```

## 可视化顺序

可视化按任务注册顺序进行：

1. DetectionTask 的可视化
2. BubbleDetectionTask 的可视化
3. MotionTask 的可视化
4. ... 其他任务

每个任务在前一个任务的可视化结果上继续绘制。

## 最佳实践

### 1. 任务独立性

每个任务应该尽可能独立，避免共享状态：

```python
# ✅ 好的做法
class MyTask(InferenceTask):
    def infer(self, frame, context):
        result = self._process(frame)
        return {"success": True, "data": result}

# ❌ 避免这样做
class MyTask(InferenceTask):
    def __init__(self):
        self.shared_state = {}  # 避免共享状态
```

### 2. 错误处理

始终捕获异常并返回错误信息：

```python
def infer(self, frame, context):
    try:
        result = self._risky_operation(frame)
        return {"success": True, "data": result}
    except Exception as e:
        print(f"Task {self.name} failed: {e}")
        return {"success": False, "error": str(e)}
```

### 3. 可视化坐标

不同任务使用不同的屏幕区域：

```python
# DetectionTask: 使用中央区域
# MotionTask: 使用左上角 (y=100)
# BubbleDetectionTask: 使用左侧 (y=150)
# CleanlinessTask: 使用右上角
```

### 4. 性能考虑

- 独立任务会并行执行，注意线程安全
- 避免在任务中使用全局变量
- 大型模型加载应在 `__init__` 中完成

## 调试

### 查看任务状态

```python
# 获取所有启用的任务
tasks = manager._task_registry.get_enabled_tasks()
for task in tasks:
    print(f"{task.name}: enabled={task.enabled}")

# 查看执行顺序
print(manager._task_registry._execution_order)
```

### 单独测试任务

```python
from app.services.example_custom_task import BubbleDetectionTask
import cv2

task = BubbleDetectionTask()
frame = cv2.imread("test.jpg")
context = {"results": {}, "task": None}

result = task.infer(frame, context)
print(result)

visual = task.visualize(frame, result)
cv2.imshow("Result", visual)
cv2.waitKey(0)
```

## 常见问题

### Q: 如何添加模型加载逻辑？

在任务的 `__init__` 方法中加载模型：

```python
class MyTask(InferenceTask):
    def __init__(self):
        super().__init__(name="my_task")
        self.model = self._load_model()
    
    def _load_model(self):
        # 加载你的模型
        return model
```

### Q: 如何在任务间传递大数据？

通过上下文的 results 字段：

```python
# 任务 A
def infer(self, frame, context):
    large_data = self._compute()
    return {"success": True, "large_data": large_data}

# 任务 B（依赖 A）
def infer(self, frame, context):
    data = context["results"]["task_a"]["large_data"]
    # 使用 data
```

### Q: 如何临时禁用某个任务？

```python
manager.enable_task("bubble_detection", enabled=False)
```

## 未来改进

- [ ] 实现完整的拓扑排序支持复杂依赖
- [ ] 添加任务优先级配置
- [ ] 支持任务级别的性能监控
- [ ] 添加任务结果缓存机制
- [ ] 支持条件执行（根据结果决定是否执行后续任务）
