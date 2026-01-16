# 流水线基类（Pipeline）设计指南 — 结构化说明

本文档整合了 `app/services/pipeline_base.py` 的设计与实现要点

文件代码参考： [app/services/pipeline_base.py](app/services/pipeline_base.py)

## 目录（快速导航）

- 核心组件与职责
- 数据流与 Cache 语义
- 主要接口（开发者须知）
- 当前实现进度
- 开发与接入流程（逐步操作）
- 示例（快速上手）
- 测试要点与最佳实践
- 优先级与后续建议

## 核心组件与职责: 自底向上三层架构

- `InferenceTask`：模型适配层，负责模型加载、预处理、单帧与批量推理 (`infer` / `infer_batch`) 及结果可视化（`visualize`）。
- `SubtaskPipelineBase`：单一**检测+分析**流水线。提供 `infer_frame` / `infer_batch` 接口；子类实现 `_infer_single_frame`（必需）和 `_infer_sequence`（可选），分别负责单帧检测和时序分析。负责维护该子任务的内部 cache 与历史。
- `TaskPipelineBase`：业务步骤层。组合多个子任务，负责运行调度（串行/并行）、存在一个异步Render，负责构建高层 `message`、维护 `state` 并提供任务级聚合后的processed_frame。

## 数据流与 Cache 语义

设计原则：把不同用途的数据分层存储，分清实时（rt）与持久化（ca）用途。

子任务级 cache（每个 `SubtaskPipelineBase`）

- `rt_cache_pos`: 实时位置/检测数据（短期、用于前端可视化）
- `ca_cache_pos`: 持久化位置数据（落盘/回放）
- `rt_cache_msg`: 实时语义消息（短期、用于前端/流转）
- `ca_cache_msg`: 持久化语义消息（用于落盘/回放/后处理）

任务级 cache（`TaskPipelineBase`）
- `rt_cache_frame` / `ca_cache_frame`: 聚合后的 `FrameData`（可视化帧）
- `rt_cache_msg` / `ca_cache_msg`: 任务级聚合消息

读写约定：Pipeline 内部负责写入；外部消费者仅读取/消费。避免在 cache 中存放不可序列化的大对象（如 numpy 大数组）。

主要接口（开发者须知）
-----------------------
Subtask 开发要点：
- 实现 `_infer_single_frame(frame, timestamp, prev_stage_cache) -> dict`（必需）：返回 JSON-serializable 字段，建议包含 `bboxes`、`score`、`meta` 等。
- 可选实现 `_infer_sequence(history) -> Optional[dict]`：用于平滑/统计，返回值将被合并到 `sequence` 字段。
- 若子任务可利用模型批量接口，应覆写 `infer_batch(frames, timestamps, prev_stage_cache)`，但请在内部使用 `_process_single_result` 或等价逻辑保证 cache 行为一致。

TaskPipeline 开发要点：
- 实现 `build_message(timestamp, subtask_results, context) -> dict`：构建业务级高层语义输出。
- 实现 `update_state(timestamp, subtask_results, message, context) -> dict`：返回步骤级 `state`，建议包含 `step_completed: bool` 与 `last_timestamp` 等字段。
- 通过构造参数控制并行（`executor` + `parallel=True`）与异步聚合（`enable_async_aggregation=True`）。

当前实现进度
----------------
- 已实现（主要功能）：
  - `SubtaskPipelineBase`：单帧入口、批量默认实现、历史队列、四类 cache、统一后处理与时序钩子。
  - `TaskPipelineBase`：子任务调度（串行/并行）、聚合输出结构、任务级 cache、可选后台异步聚合线程模板。
- 未完成/建议补充：
  - `InferenceTask` 适配层：建议封装以便模型复用与批量推理。
  - 把文档中的示例转为真实模块（例如 `app/services/examples/example_pipeline.py`）。
  - 添加单元测试覆盖关键路径（cache、并行、时序合并、异步钩子）。

开发与接入流程（逐步操作）
--------------------------------
1) 建立或复用 `InferenceTask` 适配器（可选但推荐）
   - 管理模型、设备、预处理与批量接口。

2) 实现子任务（`SubtaskPipelineBase`）
   - 继承并实现 `_infer_single_frame`，若需要实现 `_infer_sequence`。

3) 实现任务聚合（`TaskPipelineBase`）
   - 实现 `build_message` 与 `update_state`，并决定并行策略与是否启用异步聚合。

4) 本地验证
   - 运行示例驱动，观察子任务与任务级 cache 输出。
   - 验证并行路径下异常保护与 prev_stage_cache 行为。

5) 启用异步聚合（如需要）
   - 在 `_visualize_and_update_state` 中实现快速渲染与 state 更新，避免阻塞。

6) 持久化
   - 周期性将 `ca_cache_msg` / `ca_cache_frame` 写入数据库或对象存储；大型二进制存储为外部引用。

7) 测试与 CI
   - 添加单元测试并在 CI 运行，覆盖核心行为与回归检查。

示例（快速上手）
--------------------
以下为最小可运行示例（建议移动为模块并添加测试）：

```python
from concurrent.futures import ThreadPoolExecutor
import numpy as np
from app.services.pipeline_base import SubtaskPipelineBase, TaskPipelineBase

class ExampleSubtask(SubtaskPipelineBase):
    def _infer_single_frame(self, frame: np.ndarray, timestamp: float, prev_stage_cache=None):
        return {"bboxes": [[10,10,100,100]], "score": 0.9}

    def _infer_sequence(self, history):
        scores = [h.get("score", 0) for h in history]
        if not scores:
            return None
        return {"avg_score": sum(scores)/len(scores)}

class ExampleTask(TaskPipelineBase):
    def build_message(self, timestamp, subtask_results, context=None):
        alerts = []
        for name, res in subtask_results.items():
            if res and res.get("bboxes"):
                alerts.append({"task": name, "count": len(res["bboxes"])})
        return {"alerts": alerts}

    def update_state(self, timestamp, subtask_results, message, context=None):
        completed = any(res and res.get("bboxes") for res in subtask_results.values())
        return {"step_completed": bool(completed), "last_timestamp": timestamp}

executor = ThreadPoolExecutor(max_workers=2)
subtasks = [ExampleSubtask("example")]
task = ExampleTask("example_task", subtasks, executor=executor, parallel=False, enable_async_aggregation=True)
frame = np.zeros((480,640,3), dtype=np.uint8)
res = task.infer_frame(frame)
print(res)
```

测试要点与最佳实践
-----------------------
- 测试应覆盖：单帧/批量接口一致性、四类 cache 写入与裁剪、时序合并、并行异常保护、异步聚合钩子行为。
- 最佳实践：
  - 子任务返回 JSON-serializable 数据；大型二进制数据存储为外部引用。
  - 优先实现 `infer_batch` 以利用模型批量接口提高吞吐。
  - 在异步聚合钩子中避免耗时操作，将 I/O/Persist 操作交给独立线程或任务队列。

优先级建议（实现路线）
-------------------------
- 高优先级：封装 `InferenceTask` 适配层、补充示例实现与单元测试；
- 中优先级：异步聚合稳定性与性能优化（批量推理、线程阻塞分析）；
- 低优先级：抽象化 cache 持久化组件以支持多种存储后端。

下一步（可选，由我代劳）
-------------------------
- 将文档示例移为模块并添加测试（推荐起点）；
- 编写 `InferenceTask` 最小适配器并在 `ExampleSubtask` 中演示；
- 编写 `SubtaskPipelineBase` 与 `TaskPipelineBase` 的单元测试模版并运行。

---

文件： [docs/PIPELINE_BASE.md](docs/PIPELINE_BASE.md)

如果你同意我现在实现其中一项，请告诉我想先完成的任务，我会更新 TODO 并开始编码/测试。
