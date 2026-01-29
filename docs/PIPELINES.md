# CleanSight 推理流水线开发指南

本文档介绍如何为不同清洗阶段开发推理流水线。

## 已实现的推理流水线

### LEAK 阶段（泄漏检测）

**模型组**:
- `bubble_detection` - 气泡检测（YOLO）
- `bending_detection` - 内镜弯折检测（YOLO）

**配置** (`config/inference_config.yaml`):
```yaml
stages:
  LEAK:
    models:
      - name: bubble_detection
        class: app.services.models.bubble.BubbleDetectionTask
        model_path: ./app/data/bubble-best.pt
        conf_threshold: 0.5

      - name: bending_detection
        class: app.services.models.bending.EndoscopeBendingDetectionTask
        model_path: ./app/data/bend-best.pt
        conf_threshold: 0.6

    temporal_analyzer:
      config:
        bubble:
          mode: consecutive
          threshold: 3    # 连续3帧触发
        bending:
          mode: sliding_window
          window_seconds: 2.0
          ratio: 0.7      # 2秒内70%帧检测到

    alarm_triggers:
      - condition: bubble_detected == True
        message: "检测到气泡异常"
      - condition: bending_detected == True
        message: "检测到内镜弯折异常"
```

**ClientState 维护**:
```python
# 推理过程中记录时序数据
state.push_temporal_history(
    "bubble_detections",
    bubble_detected,
    timestamp=time.time()
)

# 时序分析器自动查询历史并触发告警
history = state.get_temporal_history("bubble_detections")
```

---

### CLEAN 阶段（清洁检测）

当前为空，可扩展。配置示例：

```yaml
stages:
  CLEAN:
    models: []
    temporal_analyzer:
      config: {}
    alarm_triggers: []
```

---

## 开发新流水线

### 步骤1: 创建 InferenceTask 子类

```python
# app/services/models/your_model/task.py
from app.services.infer_task import InferenceTask

class YourDetectionTask(InferenceTask):
    def __init__(self, model_path, conf_threshold=0.5, **kwargs):
        self.detector = YourDetector(model_path, conf_threshold)

    def infer(self, frame, context=None):
        """单帧推理"""
        results = self.detector.detect(frame)
        return InferenceResult(
            detections=results,
            metadata={"confidence": 0.95}
        )

    def infer_batch(self, frames, contexts=None):
        """批量推理（可选，默认使用infer）"""
        return [self.infer(f, c) for f, c in zip(frames, contexts)]

    def visualize(self, frame, result):
        """可视化检测结果"""
        annotated_frame = frame.copy()
        # 绘制检测框
        return annotated_frame
```

### 步骤2: 配置 inference_config.yaml

```yaml
stages:
  YOUR_STAGE:
    models:
      - name: your_detection
        class: app.services.models.your_model.YourDetectionTask
        params:
          model_path: ./app/data/your-model.pt
          conf_threshold: 0.6

    temporal_analyzer:
      config:
        your_detection:
          mode: consecutive      # 或 sliding_window, accumulated
          threshold: 5

    alarm_triggers:
      - condition: your_detected == True
        message: "检测到异常"
```

### 步骤3: 实现时序分析逻辑

时序分析器会自动根据配置执行，支持三种模式：

1. **consecutive**: 连续N帧触发
2. **sliding_window**: 时间窗口内比例触发
3. **accumulated**: 累计计数触发

### 步骤4: 定义告警触发条件

告警触发器支持Python表达式：

```yaml
alarm_triggers:
  - condition: your_detected == True and confidence > 0.8
    message: "高置信度检测到异常"
    severity: high
```

---

## 相关文档

- [推理服务架构](INFERENCE_SERVICE_ARCHITECTURE.md) - 推理服务设计
- [配置驱动架构](CONFIG_DRIVEN_ARCHITECTURE.md) - 配置文件详解
- [自定义任务快速开始](QUICK_START_CUSTOM_TASK.md) - 示例教程

**最后更新**: 2026-01-30
