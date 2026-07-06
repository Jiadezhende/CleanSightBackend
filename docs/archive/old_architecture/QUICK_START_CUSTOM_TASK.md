# 快速开始：扩展新的推理任务

本指南将帮助你快速创建并集成一个新的推理任务。

## 5 分钟快速开始

### 1. 创建你的任务文件

在 `app/services/ai_models/` 目录下创建新文件，例如 `stain_detection.py`:

```python
"""
污渍检测任务
"""
import cv2
import numpy as np
from typing import Dict, Any
from app.services.ai import InferenceTask, InferenceResult


class StainDetectionTask(InferenceTask):
    """检测内窥镜上的污渍"""
    
    def __init__(self):
        super().__init__(name="stain_detection", enabled=True)
        # 在这里加载你的模型（如果有）
        # self.model = load_your_model()
    
    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """执行污渍检测"""
        try:
            # TODO: 实现你的检测逻辑
            # 这里是一个简单的示例
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            _, thresh = cv2.threshold(gray, 50, 255, cv2.THRESH_BINARY_INV)
            
            # 查找轮廓
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            
            # 过滤小的轮廓
            stains = [cnt for cnt in contours if cv2.contourArea(cnt) > 100]
            
            return {
                "success": True,
                "stain_count": len(stains),
                "stains": stains
            }
        except Exception as e:
            print(f"Stain detection error: {e}")
            return {
                "success": False,
                "error": str(e),
                "stain_count": 0,
                "stains": []
            }
    
    def visualize(self, frame: np.ndarray, result: InferenceResult) -> np.ndarray:
        """可视化污渍检测结果"""
        if not result.get("success"):
            return frame
        
        result_frame = frame.copy()
        stains = result.get("stains", [])
        
        # 绘制污渍轮廓
        cv2.drawContours(result_frame, stains, -1, (0, 0, 255), 2)
        
        # 显示污渍数量
        stain_count = result.get("stain_count", 0)
        if stain_count > 0:
            cv2.putText(
                result_frame,
                f"Stains: {stain_count}",
                (10, 200),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.6,
                (0, 0, 255),
                2
            )
        
        return result_frame
    
    def requires_context(self):
        """此任务不依赖其他任务"""
        return []
```

### 2. 注册任务

编辑 `app/services/ai.py`，找到 `_register_default_tasks` 方法:

```python
def _register_default_tasks(self):
    """注册默认的推理任务"""
    self._task_registry.register(DetectionTask())
    self._task_registry.register(MotionTask())
    
    # 导入并注册你的任务
    from app.services.ai_models.stain_detection import StainDetectionTask
    self._task_registry.register(StainDetectionTask())
```

### 3. 启动并测试

```python
from app.services import ai

# 启动推理服务
ai.start()

# 提交帧进行处理
ai.submit_frame("client_1", frame)

# 获取处理结果
result = ai.get_result("client_1")
```

就这么简单！你的任务现在会与其他任务并行执行。

## 进阶：创建依赖任务

如果你的任务需要其他任务的结果，只需实现 `requires_context` 方法:

```python
class AdvancedTask(InferenceTask):
    def __init__(self):
        super().__init__(name="advanced_task", enabled=True)
    
    def requires_context(self):
        """依赖检测任务和污渍检测任务"""
        return ["detection", "stain_detection"]
    
    def infer(self, frame, context):
        # 获取依赖任务的结果
        results = context.get("results", {})
        detection_result = results.get("detection", {})
        stain_result = results.get("stain_detection", {})
        
        # 使用这些结果
        keypoints = detection_result.get("keypoints", {})
        stain_count = stain_result.get("stain_count", 0)
        
        # 执行你的逻辑
        # ...
        
        return {"success": True, "data": "..."}
```

## 常用模板

### 模板 1: 简单的目标检测

```python
class SimpleDetectionTask(InferenceTask):
    def __init__(self):
        super().__init__(name="simple_detection")
        self.model = self._load_model()
    
    def infer(self, frame, context):
        detections = self.model.detect(frame)
        return {
            "success": True,
            "detections": detections,
            "count": len(detections)
        }
    
    def visualize(self, frame, result):
        for det in result.get("detections", []):
            x, y, w, h = det["bbox"]
            cv2.rectangle(frame, (x, y), (x+w, y+h), (0, 255, 0), 2)
        return frame
```

### 模板 2: 特征提取（不需要可视化）

```python
class FeatureExtractionTask(InferenceTask):
    def __init__(self):
        super().__init__(name="feature_extraction")
    
    def infer(self, frame, context):
        features = self._extract_features(frame)
        return {
            "success": True,
            "features": features
        }
    
    def visualize(self, frame, result):
        # 特征提取不需要可视化
        return frame
```

### 模板 3: 使用多个依赖

```python
class FusionTask(InferenceTask):
    def __init__(self):
        super().__init__(name="fusion")
    
    def requires_context(self):
        return ["detection", "stain_detection", "bubble_detection"]
    
    def infer(self, frame, context):
        results = context.get("results", {})
        
        # 融合多个任务的结果
        detection = results.get("detection", {})
        stain = results.get("stain_detection", {})
        bubble = results.get("bubble_detection", {})
        
        score = self._compute_fusion_score(detection, stain, bubble)
        
        return {
            "success": True,
            "fusion_score": score
        }
```

## 调试技巧

### 1. 查看任务执行日志

在任务的 `infer` 方法中添加日志:

```python
def infer(self, frame, context):
    print(f"[{self.name}] Starting inference...")
    # 你的代码
    print(f"[{self.name}] Inference complete")
    return result
```

### 2. 单独测试任务

```python
from app.services.ai_models.stain_detection import StainDetectionTask
import cv2

# 创建任务实例
task = StainDetectionTask()

# 读取测试图像
frame = cv2.imread("test_image.jpg")

# 构造测试上下文
context = {
    "task": None,
    "results": {}
}

# 执行推理
result = task.infer(frame, context)
print(result)

# 测试可视化
visual_frame = task.visualize(frame, result)
cv2.imshow("Result", visual_frame)
cv2.waitKey(0)
```

### 3. 临时禁用其他任务

```python
from app.services import ai

# 只启用你的任务
ai.manager.enable_task("detection", enabled=False)
ai.manager.enable_task("motion", enabled=False)
ai.manager.enable_task("stain_detection", enabled=True)

ai.start()
```

## 性能优化建议

### 1. 模型预加载

在 `__init__` 中加载模型，而不是在 `infer` 中:

```python
def __init__(self):
    super().__init__(name="my_task")
    self.model = self._load_heavy_model()  # 只加载一次
```

### 2. 结果缓存

对于计算密集型任务，考虑缓存结果:

```python
def __init__(self):
    super().__init__(name="my_task")
    self._cache = {}

def infer(self, frame, context):
    frame_hash = hash(frame.tobytes())
    if frame_hash in self._cache:
        return self._cache[frame_hash]
    
    result = self._heavy_computation(frame)
    self._cache[frame_hash] = result
    return result
```

### 3. 降采样

如果不需要全分辨率:

```python
def infer(self, frame, context):
    # 降采样到 640x480
    small_frame = cv2.resize(frame, (640, 480))
    result = self._process(small_frame)
    return result
```

## 下一步

- 查看 `app/services/example_custom_task.py` 了解更多示例
- 阅读 `docs/AI_INFERENCE_ARCHITECTURE.md` 了解架构细节
- 实现你自己的模型和推理逻辑

祝你好运！🚀
