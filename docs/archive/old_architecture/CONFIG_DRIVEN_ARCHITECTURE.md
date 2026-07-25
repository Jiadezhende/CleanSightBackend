# 配置驱动的推理架构

## 概述

本文档说明如何使用配置驱动的架构来开发新的检测能力，无需修改核心推理服务代码。

**重要**：新架构已经完全解耦，不再依赖 `pipeline_base`。所有推理服务基于统一的 `InferenceTask` 基类。

## 架构设计理念

### 核心原则：完全解耦 + 配置驱动

✅ **已实现的解耦**：
- ❌ 不再依赖 `pipeline_base.SubtaskPipelineBase`
- ✅ 统一使用 `InferenceTask` 基类
- ✅ 告警系统独立于 HLS 段落盘
- ✅ 模型、时序分析器、可视化器完全独立
- ✅ 通过配置文件绑定组件关系

```
┌─────────────────────────────────────────────────────┐
│          配置文件 (stages_config.yaml)               │
│  定义：Stage → [Models] → TemporalAnalyzer          │
│        → Visualizer → AlarmTriggers                 │
└─────────────────────────────────────────────────────┘
                        ↓
┌─────────────────────────────────────────────────────┐
│          组件工厂 (ComponentFactory)                 │
│  职责：根据配置动态实例化组件                         │
└─────────────────────────────────────────────────────┘
                        ↓
┌──────────────┬──────────────────┬───────────────────┐
│  模型基类     │  时序分析器基类   │  可视化器基类      │
│  (BaseModel) │ (BaseAnalyzer)   │ (BaseVisualizer)  │
└──────────────┴──────────────────┴───────────────────┘
```

## 添加新检测能力的步骤

### 步骤 1: 实现模型基类

在 `app/services/ai_models/` 目录下创建新的模型类：

```python
# app/services/ai_models/my_new_task.py

from app.services.infer_task import InferenceTask, InferenceResult
from typing import Any, Dict
import numpy as np

class MyNewDetectionTask(InferenceTask):
    """新的检测任务"""

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.5,
        enabled: bool = True
    ):
        super().__init__(name="my_new_detection", enabled=enabled)
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        # 初始化模型
        self._load_model()

    def _load_model(self):
        """加载模型"""
        # TODO: 实现模型加载逻辑
        pass

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """单帧推理"""
        # TODO: 实现推理逻辑
        result = {
            "detected": True,
            "confidence": 0.95,
            "boxes": [[100, 100, 200, 200]]
        }
        return InferenceResult(success=True, data=result)

    def infer_batch(self, frames: list, context: Dict[str, Any]) -> list:
        """批量推理（可选，用于GPU加速）"""
        return [self.infer(frame, context) for frame in frames]
```

### 步骤 2: 实现时序分析器（可选）

如果需要自定义时序分析逻辑，在 `app/services/inference/` 目录下创建：

```python
# app/services/inference/my_temporal_analyzer.py

from app.services.inference.temporal_analyzer import BaseTemporalAnalyzer
from typing import Any, Dict, List

class MyTemporalAnalyzer(BaseTemporalAnalyzer):
    """自定义时序分析器"""

    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        # 初始化状态

    def analyze(
        self,
        task_name: str,
        result: Dict[str, Any],
        timestamp: float
    ) -> Dict[str, Any]:
        """分析单个检测结果"""
        # TODO: 实现时序分析逻辑
        return {
            "triggered": True,
            "event": "连续检测到异常",
            "confidence": 0.9
        }
```

### 步骤 3: 修改配置文件

编辑 `app/config/stages_config.yaml`：

```yaml
stages:
  # 添加新的 Stage 或在现有 Stage 中添加模型
  MY_NEW_STAGE:
    models:
      - name: my_new_detection
        class: app.services.ai_models.my_new_task.MyNewDetectionTask
        params:
          model_path: ${MY_MODEL_PATH:./weights/my_model.pt}
          conf_threshold: 0.6
          enabled: true

    temporal_analyzer:
      class: app.services.inference.my_temporal_analyzer.MyTemporalAnalyzer
      config:
        my_new_detection:
          mode: sliding_window
          window_seconds: 3.0
          ratio: 0.75

    visualizer:
      class: app.services.ai.DefaultVisualizer

    alarm_triggers:
      - condition: detected == True
        alarm_type: 自定义告警
        alarm_level: high
        alarm_message: 检测到新的异常情况
```

### 步骤 4: 重启服务

```bash
# 重启推理服务即可自动加载新配置
python main.py
```

**就这么简单！无需修改 `InferenceManager` 或其他核心代码。**

## 配置文件详解

### 配置结构

```yaml
stages:
  STAGE_NAME:
    models:           # 模型列表（按顺序执行）
      - name: model_name
        class: module.path.ClassName
        params:       # 构造函数参数
          param1: value1
          param2: value2

    temporal_analyzer:  # 时序分析器（可选）
      class: module.path.ClassName
      config:           # 时序分析配置
        task_name:
          mode: consecutive | sliding_window
          threshold: 3
          window_seconds: 2.0
          ratio: 0.7

    visualizer:       # 可视化器（可选）
      class: module.path.ClassName

    alarm_triggers:   # 告警触发条件（可选）
      - condition: expression
        alarm_type: type
        alarm_level: level
        alarm_message: message

global:               # 全局配置
  batch_size: 4
  inference_decimation: 2
  visualization_decimation: 1
  alarm:
    batch_interval: 30
    cooldown_seconds: 60
```

### 环境变量展开

配置文件支持 `${VAR_NAME}` 或 `${VAR_NAME:default}` 格式的环境变量展开：

```yaml
models:
  - name: bubble_detection
    class: app.services.ai_models.bubble_task.BubbleDetectionTask
    params:
      # 从环境变量读取，如果不存在则使用默认值
      model_path: ${BUBBLE_MODEL_PATH:./weights/bubble.pt}
      conf_threshold: 0.5
```

## 告警触发机制

### 配置告警触发条件

```yaml
alarm_triggers:
  - condition: bubble_detected == True
    alarm_type: 流程违规
    alarm_level: high
    alarm_message: 检测到气泡异常

  - condition: confidence > 0.9 and count > 5
    alarm_type: 严重异常
    alarm_level: critical
    alarm_message: 高置信度连续检测到异常
```

### 告警触发流程

1. **时序分析器** 输出 `TemporalAnalysisResult`
2. **告警触发器** 评估 `condition` 表达式
3. 如果条件满足，调用 `ai.report_alarm()` 上报告警
4. **告警管理器** 批量去重和上报

### 在代码中手动触发告警

```python
from app.services import ai

ai.report_alarm({
    'task_id': 123,
    'step_id': 1,
    'client_id': 'client_001',
    'alarm_type': '流程违规',
    'alarm_level': 'high',
    'alarm_message': '检测到异常',
    'detection_result': {'detected': True}
})
```

## 与原有架构的对比

### 原有架构（硬编码）

```python
# 添加新检测能力需要修改多处代码

# 1. 修改 InferenceTaskRegistry
self._task_registry.register(NewDetectionTask())

# 2. 修改 PipelineRegistry
new_pipeline = NewPipelineService(...)

# 3. 修改 InferenceManager._execute_inference_pipeline_batch
if task.current_step == "NEW_STAGE":
    new_pipeline.infer_batch(...)

# 4. 修改告警逻辑
if result.new_detected:
    self._handle_alarm(...)
```

### 新架构（配置驱动）

```yaml
# 只需修改配置文件

stages:
  NEW_STAGE:
    models:
      - name: new_detection
        class: app.services.ai_models.new_task.NewDetectionTask
        params:
          model_path: ./weights/new_model.pt

    alarm_triggers:
      - condition: new_detected == True
        alarm_type: 新异常
        alarm_message: 检测到新异常
```

**优势：**
- ✅ 无需修改核心代码
- ✅ 配置集中管理
- ✅ 易于调试和回滚
- ✅ 支持热加载（未来可扩展）
- ✅ 降低耦合度

## 接口规范

### 模型基类接口

```python
class InferenceTask:
    def __init__(self, name: str, enabled: bool = True):
        pass

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> InferenceResult:
        """单帧推理（必须实现）"""
        pass

    def infer_batch(self, frames: list, context: Dict[str, Any]) -> list:
        """批量推理（可选，用于GPU加速）"""
        pass

    def requires_context(self) -> bool:
        """是否需要上下文信息（可选）"""
        return False
```

### 时序分析器接口

```python
class BaseTemporalAnalyzer:
    def __init__(self, config: Dict[str, Any]):
        pass

    def analyze(
        self,
        task_name: str,
        result: Dict[str, Any],
        timestamp: float
    ) -> Dict[str, Any]:
        """分析单个检测结果（必须实现）"""
        pass

    def reset(self, task_name: str):
        """重置状态（可选）"""
        pass
```

### 可视化器接口

```python
class Visualizer:
    def visualize(
        self,
        frame: np.ndarray,
        inference_result: Dict[str, Any],
        stage: str,
        temporal_result: Optional[TemporalAnalysisResult] = None
    ) -> np.ndarray:
        """在帧上绘制检测结果（必须实现）"""
        pass
```

## 最佳实践

### 1. 模型命名规范

- 使用 `snake_case` 命名
- 名称应具有描述性：`bubble_detection`, `bending_detection`
- 避免使用缩写

### 2. 时序分析器配置

- **consecutive 模式**：适用于需要连续多帧确认的场景
  ```yaml
  config:
    bubble:
      mode: consecutive
      threshold: 3  # 连续3帧
  ```

- **sliding_window 模式**：适用于需要统计一段时间内比例的场景
  ```yaml
  config:
    bending:
      mode: sliding_window
      window_seconds: 2.0
      ratio: 0.7  # 2秒内70%的帧
  ```

### 3. 告警触发条件

- 使用简单的布尔表达式
- 避免复杂的嵌套逻辑
- 善用 `alarm_level` 区分严重程度：`low`, `medium`, `high`, `critical`

### 4. 性能优化

- 实现 `infer_batch` 方法以利用GPU批量推理
- 使用 `enabled` 参数动态启用/禁用模型
- 合理配置 `inference_decimation` 降低推理频率

## 未来扩展

### 1. 热加载配置

支持在运行时重新加载配置，无需重启服务：

```python
# 未来支持
ai.reload_config()
```

### 2. 多阶段流水线

支持更复杂的多阶段流水线：

```yaml
pipeline:
  - stage: PREPROCESSING
    models: [...]

  - stage: DETECTION
    models: [...]
    depends_on: [PREPROCESSING]

  - stage: POSTPROCESSING
    models: [...]
    depends_on: [DETECTION]
```

### 3. 动态告警规则

支持更复杂的告警规则引擎：

```yaml
alarm_rules:
  - name: bubble_rule
    conditions:
      - bubble_detected == True
      - confidence > 0.8
      - duration > 2.0
    actions:
      - type: report
        alarm_type: 流程违规
      - type: notification
        channels: [email, sms]
```

## 总结

通过配置驱动的架构，我们实现了：

1. ✅ **完全解耦**：模型、时序分析器、可视化器互不依赖
2. ✅ **易于扩展**：添加新能力只需实现基类 + 修改配置
3. ✅ **集中管理**：所有配置集中在 `stages_config.yaml`
4. ✅ **灵活部署**：通过环境变量支持不同环境的配置
5. ✅ **向后兼容**：保留原有 API 接口，平滑迁移

**未来开发新检测能力时，只需三步：**
1. 实现模型基类
2. （可选）实现时序分析器
3. 修改配置文件

**无需修改任何核心推理服务代码！**
