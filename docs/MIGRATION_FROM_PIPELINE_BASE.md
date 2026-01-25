# 从 Pipeline Base 迁移到配置驱动架构

## 概述

本文档说明如何将基于 `pipeline_base.SubtaskPipelineBase` 的旧代码迁移到新的配置驱动架构。

**关键变化**：
- ❌ 移除依赖：不再使用 `pipeline_base.SubtaskPipelineBase`
- ✅ 统一基类：所有模型基于 `InferenceTask`
- ✅ 配置驱动：通过 YAML 配置文件绑定组件
- ✅ 完全解耦：模型、时序分析器、可视化器独立开发

## 迁移步骤

### 步骤 1: 将 SubtaskPipelineBase 转换为 InferenceTask

#### 旧代码（基于 SubtaskPipelineBase）

```python
from app.services.pipeline_base import SubtaskPipelineBase

class BubbleSubtask(SubtaskPipelineBase):
    def __init__(self, model_path: str):
        super().__init__(name="bubble")
        self.model = load_model(model_path)

    def infer_batch(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
        prev_stage_cache: Optional[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """批量推理"""
        results = []
        for frame in frames:
            result = self.model.predict(frame)
            results.append({
                "bubble_detected": result.detected,
                "confidence": result.confidence,
                "boxes": result.boxes
            })
        return results
```

#### 新代码（基于 InferenceTask）

```python
from app.services.infer_task import InferenceTask, InferenceResult

class BubbleDetectionTask(InferenceTask):
    """气泡检测任务"""

    def __init__(
        self,
        model_path: str,
        conf_threshold: float = 0.5,
        enabled: bool = True
    ):
        super().__init__(name="bubble_detection", enabled=enabled)
        self.model_path = model_path
        self.conf_threshold = conf_threshold
        self._load_model()

    def _load_model(self):
        """加载模型"""
        self.model = load_model(self.model_path)

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> Dict[str, Any]:
        """单帧推理"""
        result = self.model.predict(frame)
        return {
            "success": True,
            "bubble_detected": result.detected,
            "confidence": result.confidence,
            "boxes": result.boxes
        }

    def infer_batch(
        self,
        frames: List[np.ndarray],
        contexts: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        """批量推理（可选，用于GPU加速）"""
        # 使用模型的批量推理接口
        results = self.model.predict_batch(frames)
        return [
            {
                "success": True,
                "bubble_detected": r.detected,
                "confidence": r.confidence,
                "boxes": r.boxes
            }
            for r in results
        ]

    def visualize(self, frame: np.ndarray, result: Dict[str, Any]) -> np.ndarray:
        """可视化（可选，也可以使用独立的 Visualizer）"""
        if not result.get("bubble_detected"):
            return frame

        annotated = frame.copy()
        for box in result.get("boxes", []):
            x1, y1, x2, y2 = box
            cv2.rectangle(annotated, (x1, y1), (x2, y2), (0, 0, 255), 2)
        return annotated
```

#### 主要变化

| 项目 | 旧代码 | 新代码 |
|------|--------|--------|
| 基类 | `SubtaskPipelineBase` | `InferenceTask` |
| 构造函数 | `__init__(name)` | `__init__(name, enabled)` |
| 单帧推理 | 无 | `infer(frame, context)` |
| 批量推理 | `infer_batch(frames, timestamps, prev_stage_cache)` | `infer_batch(frames, contexts)` |
| 可视化 | 内嵌在 pipeline 中 | `visualize(frame, result)` 方法 |
| 上下文 | `prev_stage_cache` | `context` 字典 |

### 步骤 2: 更新配置文件

#### 旧代码（硬编码在代码中）

```python
# 在 InferenceManager 或 PipelineRegistry 中硬编码
bubble_subtask = BubbleSubtask(model_path="./weights/bubble.pt")
bending_subtask = BendingSubtask(model_path="./weights/bending.pt")

pipeline = LeakBubblePipelineService(
    bubble_task=bubble_subtask,
    bending_task=bending_subtask
)
```

#### 新代码（配置文件驱动）

创建 `app/config/stages_config.yaml`：

```yaml
stages:
  LEAK:
    models:
      - name: bubble_detection
        class: app.services.ai_models.bubble_task.BubbleDetectionTask
        params:
          model_path: ${BUBBLE_MODEL_PATH:./weights/bubble.pt}
          conf_threshold: 0.5
          enabled: true

      - name: bending_detection
        class: app.services.ai_models.yolo_task.EndoscopeBendingDetectionTask
        params:
          model_path: ${YOLO_MODEL_PATH:./weights/yolo.pt}
          conf_threshold: 0.6
          enabled: true
```

然后在代码中加载：

```python
from app.services.inference.config_loader import load_stage_config
from app.services.inference.component_factory import ComponentFactory

# 加载配置
config = load_stage_config()
factory = ComponentFactory(config)

# 自动创建所有模型
models = factory.create_models_for_stage("LEAK")
```

### 步骤 3: 迁移 Pipeline 逻辑到时序分析器

#### 旧代码（Pipeline 混合推理+时序）

```python
class LeakBubblePipelineService:
    def __init__(self, bubble_task, bending_task):
        self.bubble_task = bubble_task
        self.bending_task = bending_task
        self.bubble_count = 0  # 时序状态

    def infer_batch(self, frames, timestamps, context):
        # 推理
        bubble_results = self.bubble_task.infer_batch(frames, timestamps, None)
        bending_results = self.bending_task.infer_batch(frames, timestamps, None)

        # 时序分析（耦合在一起）
        for bubble_res in bubble_results:
            if bubble_res["bubble_detected"]:
                self.bubble_count += 1
            else:
                self.bubble_count = 0

            # 触发告警
            if self.bubble_count >= 3:
                self._trigger_alarm("检测到气泡")

        # 返回结果
        return {"bubble": bubble_results, "bending": bending_results}
```

#### 新代码（推理与时序分离）

**推理部分**（自动由 `MultiModelWorkerPool` 处理）：

```python
# 不需要手动编写，配置文件即可
```

**时序分析部分**（独立的 `TemporalAnalyzer`）：

```python
from app.services.inference.temporal_analyzer import BaseTemporalAnalyzer

class LeakTemporalAnalyzer(BaseTemporalAnalyzer):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.bubble_count = {}  # client_id -> count

    def analyze(
        self,
        client_id: str,
        task_name: str,
        result: Dict[str, Any],
        timestamp: float
    ) -> Dict[str, Any]:
        """分析单个检测结果"""
        if task_name == "bubble_detection":
            if result.get("bubble_detected"):
                self.bubble_count[client_id] = self.bubble_count.get(client_id, 0) + 1
            else:
                self.bubble_count[client_id] = 0

            # 判断是否触发
            if self.bubble_count.get(client_id, 0) >= 3:
                return {
                    "triggered": True,
                    "event": "连续检测到气泡",
                    "alarm": {
                        "alarm_type": "流程违规",
                        "alarm_message": "检测到气泡（连续3帧）"
                    }
                }

        return {"triggered": False}
```

在配置文件中绑定：

```yaml
stages:
  LEAK:
    temporal_analyzer:
      class: app.services.inference.temporal_analyzer.LeakTemporalAnalyzer
      config:
        bubble:
          mode: consecutive
          threshold: 3
```

### 步骤 4: 迁移告警逻辑

#### 旧代码（混合在 Pipeline 中）

```python
class LeakBubblePipelineService:
    def _trigger_alarm(self, message: str):
        # 告警逻辑耦合在 Pipeline 中
        alarm_info = {
            "alarm_type": "流程违规",
            "alarm_message": message,
            # ...
        }
        # 直接调用数据库或HTTP接口
        self._send_alarm(alarm_info)
```

#### 新代码（独立的告警系统）

```python
from app.services import ai

# 在时序分析器中触发告警
def analyze(self, ...):
    if self.bubble_count >= 3:
        # 调用统一的告警接口
        ai.report_alarm({
            "task_id": task_id,
            "step_id": step_id,
            "client_id": client_id,
            "alarm_type": "流程违规",
            "alarm_message": "检测到气泡（连续3帧）",
            "detection_result": {"bubble_detected": True}
        })
```

告警系统会自动处理：
- ✅ 批量去重（30秒间隔）
- ✅ 冷却机制（60秒内不重复）
- ✅ 异步上报（不阻塞推理）
- ✅ HTTP重试（3次）
- ✅ 数据库记录

### 步骤 5: 更新调用代码

#### 旧代码（直接使用 Pipeline）

```python
# 在 InferenceManager 中
pipeline = self._get_or_create_leak_pipeline(client_id, task)
pipeline.infer_batch(frames, timestamps, context)

# 手动从 pipeline 取结果
rt_cache = pipeline.rt_cache_frame
while rt_cache:
    fd = rt_cache.popleft()
    client_queues.append_rt_processed(fd)
```

#### 新代码（使用推理服务）

```python
# 在 InferenceManager.__init__ 中
from app.services.inference.factory import create_model_worker_service_from_manager

self._model_worker_service = create_model_worker_service_from_manager(
    stage_configs=self._get_stage_configs(),
    max_batch_per_stage=8,
    use_cuda_stream=True,
    num_worker_threads=2,
)

# 启动服务
self._model_worker_service.start()

# 服务会自动：
# 1. 从 ClientQueues 读取帧
# 2. 执行推理
# 3. 执行时序分析
# 4. 执行可视化
# 5. 写回结果到 ClientQueues
```

## 完整迁移示例

### 旧架构文件结构

```
app/services/
├── ai.py                           # InferenceManager (混合逻辑)
├── pipeline_base.py                # SubtaskPipelineBase 基类
└── task_pipeline/
    └── leak/
        ├── leak_test.py           # LeakBubblePipelineService
        ├── bubble_subtask.py      # BubbleSubtask (继承 SubtaskPipelineBase)
        └── bending_subtask.py     # BendingSubtask (继承 SubtaskPipelineBase)
```

### 新架构文件结构

```
app/
├── config/
│   └── stages_config.yaml         # 配置文件（核心）
├── services/
│   ├── ai.py                       # InferenceManager (只负责调度)
│   ├── infer_task.py              # InferenceTask 基类
│   ├── ai_models/
│   │   ├── bubble_task.py         # BubbleDetectionTask (继承 InferenceTask)
│   │   └── yolo_task.py           # BendingDetectionTask (继承 InferenceTask)
│   └── inference/
│       ├── config_loader.py       # 配置加载器
│       ├── component_factory.py   # 组件工厂
│       ├── temporal_analyzer.py   # 时序分析器
│       ├── service.py             # ModelWorkerService
│       └── worker_pool.py         # MultiModelWorkerPool
└── docs/
    ├── CONFIG_DRIVEN_ARCHITECTURE.md
    └── MIGRATION_FROM_PIPELINE_BASE.md
```

## 迁移检查清单

- [ ] 将 `SubtaskPipelineBase` 子类转换为 `InferenceTask` 子类
- [ ] 实现 `infer()` 方法（单帧推理）
- [ ] 实现 `infer_batch()` 方法（批量推理，可选）
- [ ] 实现 `visualize()` 方法（可视化，可选）
- [ ] 创建配置文件 `stages_config.yaml`
- [ ] 将模型配置添加到 `stages.STAGE_NAME.models`
- [ ] 将时序分析逻辑提取到独立的 `TemporalAnalyzer`
- [ ] 配置时序分析器到 `stages.STAGE_NAME.temporal_analyzer`
- [ ] 将告警逻辑改为调用 `ai.report_alarm()`
- [ ] 配置告警触发条件到 `stages.STAGE_NAME.alarm_triggers`
- [ ] 移除对 `pipeline_base` 的导入
- [ ] 移除对 `LeakBubblePipelineService` 等 Pipeline 类的使用
- [ ] 测试新架构是否正常工作

## 常见问题

### Q: 如何处理模型之间的依赖？

**旧方式**：在 Pipeline 中手动传递 `prev_stage_cache`

**新方式**：使用 `context` 参数

```python
class MyTask(InferenceTask):
    def requires_context(self) -> List[str]:
        """声明依赖的其他任务"""
        return ["bubble_detection"]

    def infer(self, frame: np.ndarray, context: Dict[str, Any]) -> Dict[str, Any]:
        # 从 context 中获取依赖任务的结果
        bubble_result = context.get("results", {}).get("bubble_detection", {})

        # 使用依赖结果进行推理
        if bubble_result.get("bubble_detected"):
            # ...
```

### Q: 如何共享状态（例如帧间的跟踪）？

**旧方式**：在 Pipeline 对象中保存状态

**新方式**：在 `TemporalAnalyzer` 中保存状态

```python
class MyTemporalAnalyzer(BaseTemporalAnalyzer):
    def __init__(self, config: Dict[str, Any]):
        super().__init__(config)
        self.tracker = {}  # client_id -> tracker_state

    def analyze(self, client_id: str, task_name: str, result: Dict[str, Any], timestamp: float):
        # 使用 client_id 区分不同客户端的状态
        if client_id not in self.tracker:
            self.tracker[client_id] = initialize_tracker()

        # 更新跟踪器
        self.tracker[client_id].update(result)
```

### Q: 性能会下降吗？

**不会**，反而可能更快：

- ✅ **CUDA Stream 并行**：多个模型同时推理
- ✅ **批量推理**：利用 GPU 批处理
- ✅ **异步管道**：推理、时序、可视化并行执行
- ✅ **降帧推理**：可配置推理频率

### Q: 如何调试？

**新架构提供更好的调试体验**：

```python
# 1. 单独测试模型
from app.services.ai_models.bubble_task import BubbleDetectionTask

model = BubbleDetectionTask(model_path="./weights/bubble.pt")
result = model.infer(frame, context={})
print(result)

# 2. 单独测试时序分析器
from app.services.inference.temporal_analyzer import DefaultTemporalAnalyzer

analyzer = DefaultTemporalAnalyzer(config={...})
result = analyzer.analyze("bubble", {"detected": True}, timestamp=time.time())
print(result)

# 3. 查看配置
from app.services.inference.config_loader import load_stage_config

config = load_stage_config()
print(config.get_stage_config("LEAK"))
```

## 总结

新架构的优势：

| 维度 | 旧架构 | 新架构 |
|------|--------|--------|
| 耦合度 | 高（Pipeline 混合所有逻辑） | 低（组件完全独立） |
| 扩展性 | 需修改多处代码 | 只需修改配置文件 |
| 可测试性 | 难以单独测试 | 每个组件独立测试 |
| 配置管理 | 分散在代码中 | 集中在配置文件 |
| 告警系统 | 混合在 Pipeline 中 | 独立的告警管理器 |
| 性能 | 顺序执行 | CUDA Stream 并行 |
| 可维护性 | 难以理解和维护 | 清晰的分层架构 |

**建议**：优先迁移到新架构，享受配置驱动和完全解耦带来的便利！
