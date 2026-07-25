# 推理服务模块设计架构

## 概述

推理服务模块 (`app.services.inference`) 是一个高性能的多客户端、多模型并行推理框架，专为多路视频流实时推理设计。

### 核心特性

- **Stage-Aware 调度**: 根据客户端当前阶段（LEAK/CLEAN/etc.）动态分发到对应模型池
- **CUDA Stream 并行**: 每个 stage 的多个模型使用独立 CUDA Stream 真正并行推理
- **批处理优化**: 同 stage 的帧组成 batch，提升 GPU 利用率
- **ClientManager 集成**: 自动从 `ClientManager` 获取客户端列表
- **ClientState 管理**: 自动更新客户端业务状态（步骤完成、计数器等）

### 设计目标

1. **高性能**: 支持 20+ 路视频流同时推理
2. **可扩展**: 易于添加新的 stage、模型和调度策略
3. **可维护**: 模块化设计，职责清晰
4. **易用**: 提供便捷的工厂函数，开箱即用

---

## 架构图

### 整体数据流

```
┌─────────────────────────────────────────────────────────────────┐
│                        多路视频流输入                            │
│  Client 1, Client 2, ..., Client N (N=20)                       │
└──────────────────────┬──────────────────────────────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │   ClientQueues (per-stream)  │  ca_ready 队列
        │   - ca_ready (inference_fps)  │  (降帧采样后)
        │   - ca_raw                    │
        │   - ca_processed              │
        │   - rt_processed              │
        └──────────────┬─────────────────┘
                       │
                       ▼
        ┌──────────────────────────────┐
        │  StageAwareDispatcher        │  按 Stage 分组
        │  - Round-Robin 轮询          │
        │  - Stage 识别 (LEAK/CLEAN)   │
        │  - 批量取帧                   │
        └──────────────┬───────────────┘
                       │
         ┌─────────────┴─────────────┐
         │                           │
         ▼                           ▼
┌────────────────┐         ┌────────────────┐
│ Stage: LEAK    │         │ Stage: CLEAN   │
│ Queue (deque)  │         │ Queue (deque)  │
└────────┬───────┘         └────────┬───────┘
         │                           │
         ▼                           ▼
┌──────────────────────┐   ┌──────────────────────┐
│ MultiModelWorkerPool │   │ MultiModelWorkerPool │
│ - Bubble (Stream 0)  │   │ - Brush (Stream 0)   │
│ - Bending (Stream 1) │   │ - Quality (Stream 1) │
│ CUDA Stream 并行     │   │ CUDA Stream 并行      │
└──────────┬───────────┘   └──────────┬───────────┘
           │                          │
           ▼                          ▼
    ┌──────────────────────────────────┐
    │  回写到 ClientQueues              │
    │  - ca_processed (可视化后)        │
    │  - rt_processed (实时流)          │
    │  - 更新 ClientState (业务状态)    │
    └──────────────────────────────────┘
```

### 模块结构

```
app/services/inference/
├── __init__.py              # 模块入口，导出公共接口
├── models.py                # 数据模型
├── dispatcher.py            # 调度器
├── worker_pool.py           # 推理池
├── service.py               # 服务管理
└── factory.py               # 工厂函数
```

---

## 核心组件

### 1. 数据模型 (`models.py`)

#### InferenceRequest

推理请求数据结构，封装待推理的帧和元数据。

```python
@dataclass
class InferenceRequest:
    client_id: str           # 客户端标识
    frame: np.ndarray        # 原始帧（NumPy 数组）
    timestamp: float         # 时间戳
    stage: str               # 当前阶段（LEAK/CLEAN/etc.）
    frame_data: FrameData    # 原始 FrameData（用于回写）
```

#### InferenceResult

推理结果数据结构，包含所有子任务的推理结果。

```python
@dataclass
class InferenceResult:
    client_id: str                        # 客户端标识
    timestamp: float                      # 时间戳
    stage: str                            # 当前阶段
    result: Dict[str, Any]                # 子任务推理结果字典
    annotated_frame: Optional[np.ndarray] # 可视化后的帧（可选）
```

**result 结构示例**：
```python
{
    "bubble": {
        "bubble_detected": True,
        "boxes": [...],
        "scores": [...]
    },
    "bending": {
        "bending_detected": False,
        "angle": 12.5
    }
}
```

---

### 2. 调度器 (`dispatcher.py`)

#### StageAwareDispatcher

**职责**：
- 轮询所有客户端的 `ca_ready` 队列
- 识别每个客户端当前所处的 stage
- 按 stage 分组批量取帧
- 保证流间公平（Round-Robin）

**关键方法**：

```python
class StageAwareDispatcher:
    def __init__(
        self,
        client_queues_map: Dict[str, ClientQueues],
        max_batch_per_stage: int = 8,
        fetch_interval: float = 0.01,  # 10ms 轮询间隔
    )

    def start(self) -> None:
        """启动调度线程"""

    def stop(self) -> None:
        """停止调度线程"""

    def get_batch_for_stage(
        self,
        stage: str,
        max_size: int = None
    ) -> List[InferenceRequest]:
        """获取指定 stage 的一个 batch"""

    def _get_client_stage(
        self,
        client_id: str,
        cq: ClientQueues
    ) -> str:
        """获取客户端当前所处的 stage"""
```

**Stage 识别逻辑**：
1. 优先从 `ClientState.get_stage()` 读取（新架构）
2. 兼容从 `task.current_step` 推断（旧架构）
3. 默认返回 "LEAK"

**调度策略**：
- Round-Robin 轮询所有客户端
- 从每个客户端的 `ca_ready` 队列取一帧
- 按 stage 分组存入对应队列
- 保证公平性：每个客户端每轮最多取 1 帧

---

### 3. 推理池 (`worker_pool.py`)

#### MultiModelWorkerPool

**职责**：
- 管理单个 stage 的多个模型（2-3 个）
- 为每个模型分配独立的 CUDA Stream
- 批量推理，支持并行和顺序两种模式

**关键方法**：

```python
class MultiModelWorkerPool:
    def __init__(
        self,
        stage: str,
        subtasks: Sequence[SubtaskPipelineBase],
        use_cuda_stream: bool = True,
    )

    def infer_batch(
        self,
        batch: List[InferenceRequest]
    ) -> List[InferenceResult]:
        """批量推理：多个模型并行执行"""

    def _infer_batch_parallel_cuda(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[Dict[str, Dict[str, Any]]]:
        """CUDA Stream 并行推理"""

    def _infer_batch_sequential(
        self,
        frames: List[np.ndarray],
        timestamps: List[float],
    ) -> List[Dict[str, Dict[str, Any]]]:
        """顺序推理（不使用 CUDA Stream）"""
```

**CUDA Stream 并行原理**：

```python
# 为每个子任务启动异步推理
for subtask, cuda_stream in zip(self.subtasks, self.cuda_streams):
    with torch.cuda.stream(cuda_stream):
        batch_res = subtask.infer_batch(frames, timestamps)
        async_results.append((subtask.name, batch_res))

# 同步所有 CUDA Stream
torch.cuda.synchronize()
```

**性能优化**：
- CUDA Stream 并行可获得 10-30% 性能提升（取决于模型大小）
- 批处理减少 CPU-GPU 数据传输开销
- 异步执行最大化 GPU 利用率

---

### 4. 服务管理 (`service.py`)

#### ModelWorkerService

**职责**：
- 统一管理 Dispatcher 和 WorkerPool
- 为每个 stage 创建独立的推理线程
- 回写推理结果到 ClientQueues
- 更新 ClientState 业务状态

**关键方法**：

```python
class ModelWorkerService:
    def __init__(
        self,
        client_queues_map: Optional[Dict[str, ClientQueues]] = None,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        max_batch_per_stage: int = 8,
        use_cuda_stream: bool = True,
        num_worker_threads: int = 2,
        client_manager_instance: Optional[ClientManager] = None,
    )

    def start(self) -> None:
        """启动服务：Dispatcher + 推理线程"""

    def stop(self) -> None:
        """停止服务"""

    def refresh_client_queues(self) -> None:
        """刷新客户端队列映射（动态添加客户端时调用）"""

    def _inference_loop(self, stage: str) -> None:
        """推理循环：消费指定 stage 的批量请求"""

    def _write_back_results(
        self,
        results: List[InferenceResult]
    ) -> None:
        """将推理结果回写到 ClientQueues"""

    def _update_client_state(
        self,
        state: ClientState,
        result: InferenceResult
    ) -> None:
        """更新客户端业务状态（可由子类覆写）"""

    def _visualize_result(
        self,
        result: InferenceResult,
        cq: ClientQueues,
    ) -> Optional[np.ndarray]:
        """可视化推理结果（可由子类或外部覆写）"""
```

**线程模型**：
- 1 个 Dispatcher 线程（轮询取帧）
- N 个 InferWorker 线程（每个 stage 一个）
- 使用 ThreadPoolExecutor 管理线程池

#### InferWorker 详细职责

每个 InferWorker 线程对应一个 stage（如 LEAK、CLEAN），负责处理该 stage 的所有客户端流。

**工作流程**（`_inference_loop`）：

```python
while not stopped:
    # 1. 从 Dispatcher 获取批量请求
    batch = dispatcher.get_batch_for_stage(stage, max_size=batch_size)

    # 2. 批量推理（CUDA Stream 并行）
    results = worker_pool.infer_batch(batch)  # 多模型并行执行

    # 3. 结果回写（循环处理每个结果）
    for result in results:
        # 3.1 更新 ClientState（业务状态）
        _update_client_state(state, result)

        # 3.2 可视化处理（绘制检测框/标注）
        annotated_frame = _visualize_result(result, cq)

        # 3.3 写入队列
        cq.append_ca_processed(frame_data)
        cq.append_rt_processed(frame_data)

    # 4. 性能统计
    print(f"batch_size={len(batch)}, 耗时={elapsed}ms, 吞吐={fps}fps")
```

**职责拆解**：

| 阶段 | 操作 | 耗时估计 | 并行性 | 瓶颈分析 |
|-----|------|---------|--------|---------|
| 1. 消费队列 | 从 Dispatcher 取 batch | < 1ms | ❌ 串行 | ✅ 极快 |
| 2. 批量推理 | MultiModelWorkerPool.infer_batch() | 20-50ms | ✅ CUDA Stream 并行 | ✅ 已优化 |
| 3.1 状态更新 | ClientState 计数器/标志位更新 | 1-2ms/batch | ❌ 串行 | ⚠️ 轻量 |
| 3.2 可视化 | CPU 绘制检测框/标注 | 15-30ms/batch | ❌ 串行 | ⚠️ CPU 密集 |
| 3.3 队列写入 | 写入 ca_processed/rt_processed | 1-2ms/batch | ❌ 串行 | ⚠️ 轻量 |

**性能特征**：
- **推理部分**（阶段 2）：已通过 CUDA Stream 实现并行，吞吐高
- **后处理部分**（阶段 3）：串行执行，可能成为瓶颈
  - 可视化处理（绘制边界框、标注）是 CPU 密集型操作
  - 处理 20 路视频流时，后处理可能占总耗时的 40-50%

**当前设计的考量**：
- ✅ **简单直接**：推理 → 回写在同一线程，易于理解和调试
- ✅ **数据一致性**：状态更新与推理结果强绑定，无需额外同步
- ⚠️ **后处理成为瓶颈**：可视化、状态更新串行执行，限制吞吐
- ⚠️ **单 stage 单线程**：一个 InferWorker 处理该 stage 的所有流

**潜在优化方向**（未来考虑）：
1. **异步回写线程池**：将结果回写从推理线程分离，提升 40-50% 吞吐
2. **可视化异步化**：仅将耗时的可视化操作异步化，轻量改造
3. **多 InferWorker per Stage**：高负载场景（30+ 路流）启动多个 worker

**状态更新示例**：

```python
def _update_client_state(self, state: ClientState, result: InferenceResult):
    if result.stage == "LEAK":
        bubble_res = result.result.get("bubble", {})
        if bubble_res.get("bubble_detected"):
            count = state.increment_counter("continuous_bubble")
            if count >= 3:  # 连续 3 帧检测到气泡
                state.mark_step_completed()
        else:
            state.reset_counter("continuous_bubble")
```

---

### 5. 工厂函数 (`factory.py`)

#### create_model_worker_service_from_manager

从 `ClientManager` 自动创建服务（推荐方式）。

```python
def create_model_worker_service_from_manager(
    stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
    max_batch_per_stage: int = 8,
    use_cuda_stream: bool = True,
    num_worker_threads: int = 2,
) -> ModelWorkerService
```

**使用示例**：

```python
from app.services.inference import create_model_worker_service_from_manager

# 创建服务（自动从 ClientManager 获取所有客户端）
service = create_model_worker_service_from_manager()

# 启动
service.start()

# 动态添加客户端后刷新
service.refresh_client_queues()

# 停止
service.stop()
```

#### create_model_worker_service_example

手动提供 `client_queues_map` 创建服务。

```python
def create_model_worker_service_example(
    client_queues_map: Dict[str, ClientQueues]
) -> ModelWorkerService
```

#### _create_default_stage_configs

创建默认的 Stage 配置（LEAK 阶段）。

```python
stage_configs = {
    "LEAK": {
        "subtasks": [
            BubbleSubtaskPipeline(name="bubble", task=bubble_task),
            BendingSubtaskPipeline(name="bending", task=bending_task),
        ],
        "batch_size": 4,
    },
}
```

---

## Stage 配置详解

### 配置结构

```python
stage_configs = {
    "<stage_name>": {
        "subtasks": [<SubtaskPipeline>, ...],  # 子任务列表
        "batch_size": <int>,                    # 批量大小
    },
}
```

### 添加新 Stage 示例

```python
from app.services.task_pipeline.clean.clean_subtasks import (
    BrushSubtaskPipeline,
    QualitySubtaskPipeline,
)

stage_configs = {
    "LEAK": {
        "subtasks": [
            BubbleSubtaskPipeline(name="bubble", task=bubble_task),
            BendingSubtaskPipeline(name="bending", task=bending_task),
        ],
        "batch_size": 4,
    },
    "CLEAN": {
        "subtasks": [
            BrushSubtaskPipeline(name="brush", task=brush_task),
            QualitySubtaskPipeline(name="quality", task=quality_task),
        ],
        "batch_size": 6,
    },
}

service = create_model_worker_service_from_manager(stage_configs=stage_configs)
```

### Batch Size 调优建议

| 模型类型 | 推荐 Batch Size | 说明 |
|---------|----------------|------|
| 轻量模型 (YOLOv8n) | 4-8 | 降低延迟，提升吞吐 |
| 中等模型 (YOLOv8m) | 2-4 | 平衡延迟和显存 |
| 大型模型 (YOLOv8x) | 1-2 | 避免显存溢出 |

---

## 性能优化

### 1. CUDA Stream 并行

**开启条件**：
- 有多个轻量级模型（2-3 个）
- GPU 未满载（显存和计算资源充足）
- 模型推理时间 < 50ms

**对比测试**：

```python
# 测试并行
service_parallel = create_model_worker_service_from_manager(use_cuda_stream=True)

# 测试顺序
service_sequential = create_model_worker_service_from_manager(use_cuda_stream=False)
```

**性能提升**：
- 2 个模型并行：10-20% 提升
- 3 个模型并行：15-30% 提升

### 2. 批处理优化

**原理**：
- 将多个客户端的帧组成 batch
- 减少 CPU-GPU 数据传输次数
- 提升 GPU 利用率

**配置**：

```python
service = create_model_worker_service_from_manager(
    max_batch_per_stage=8,  # 全局最大 batch
)

# 也可以为每个 stage 单独配置
stage_configs = {
    "LEAK": {"batch_size": 4},   # LEAK stage 使用 batch_size=4
    "CLEAN": {"batch_size": 8},  # CLEAN stage 使用 batch_size=8
}
```

### 3. 推理帧率控制

在 `ClientQueues` 初始化时设置：

```python
cq = ClientQueues(
    client_id="client_1",
    inference_fps=10,  # 10fps 推理，降低 GPU 负载
)
```

### 4. 调度间隔调优

```python
dispatcher = StageAwareDispatcher(
    client_queues_map=client_queues_map,
    fetch_interval=0.01,  # 10ms 轮询间隔（默认）
)
```

- 间隔过小：CPU 占用高
- 间隔过大：延迟增加

---

## 监控和调试

### 队列深度监控

```python
from app.services.client_manager import client_manager

# 获取所有客户端的队列深度
depths = client_manager.get_all_queue_depths()
# 输出：
# {
#     "client_1": {"ca_ready": 5, "ca_raw": 10, ...},
#     "client_2": {"ca_ready": 3, "ca_raw": 8, ...},
# }

# 获取 Dispatcher 的 stage 队列深度
stage_depths = service.dispatcher.get_stage_queue_depths()
# 输出: {"LEAK": 10, "CLEAN": 5}
```

### 推理统计

推理线程会自动打印统计信息：

```
[InferWorker-LEAK] 完成 batch_size=4, 耗时=45.2ms, 吞吐=88.5fps
[InferWorker-CLEAN] 完成 batch_size=6, 耗时=62.8ms, 吞吐=95.5fps
```

---

## 时序分析架构

### 设计原则

弃用原有的 `pipeline_base.py` 时序处理设计，**所有时序分析逻辑统一在 inference 模块中实现**。

**核心理念**：
- `ClientState` 维护时序状态（计数器、历史队列）
- `InferWorker` 实现时序决策逻辑（业务规则）
- 通过 `_update_client_state()` 钩子实现自定义时序规则

### 实现层次

```
┌──────────────────────────────────────────────────┐
│   ModelWorkerService._update_client_state()      │
│   - 决策层：根据推理结果实现业务规则              │
│   - 可由子类覆写，实现自定义时序逻辑              │
└───────────────────┬──────────────────────────────┘
                    │ 调用
                    ▼
┌──────────────────────────────────────────────────┐
│   ClientState (app/services/client.py)           │
│   - 状态管理层：维护时序计数器和历史队列          │
│   - 线程安全的状态更新接口                       │
│   - 提供 increment_counter/reset_counter 等方法   │
└──────────────────────────────────────────────────┘
```

### ClientState 时序功能

`ClientState` 提供两类时序统计功能：

#### 1. 时序计数器（Sequence Counters）

用于连续帧检测和累计计数：

```python
# 递增计数器
count = state.increment_counter("continuous_bubble", delta=1)

# 获取计数器值
count = state.get_counter("continuous_bubble", default=0)

# 重置计数器
state.reset_counter("continuous_bubble")
```

**内部存储**：
```python
_sequence_counters: Dict[str, int] = {
    "continuous_bubble": 5,      # 连续检测到气泡的帧数
    "continuous_clean": 3,        # 连续清洁的帧数
    "bending_count": 12,          # 累计折弯检测次数
}
```

#### 2. 历史队列（可选扩展）

用于滑动窗口统计，需要扩展 `ClientState`：

```python
class ClientState:
    def __init__(self, ...):
        self._history_queues: Dict[str, deque] = {}

    def push_to_history(self, key: str, value: Any, max_len: int = 10):
        """追加到历史队列（滑动窗口）"""
        with self._lock:
            if key not in self._history_queues:
                self._history_queues[key] = deque(maxlen=max_len)
            self._history_queues[key].append(value)

    def get_history(self, key: str) -> List[Any]:
        """获取历史队列"""
        with self._lock:
            return list(self._history_queues.get(key, []))
```

### 时序分析模式

#### 模式 1: 连续帧检测（Consecutive Frames）

**用途**：要求连续 N 帧都满足条件

**实现**：
```python
def _update_client_state(self, state, result):
    detected = result.result.get("bubble", {}).get("bubble_detected")

    if detected:
        count = state.increment_counter("continuous_bubble")
        if count >= 3:  # 连续 3 帧阈值
            state.mark_step_completed()
    else:
        state.reset_counter("continuous_bubble")  # 中断则重置
```

**适用场景**：
- 气泡检测（连续 3 帧有气泡才判定为真实）
- 清洁度判断（连续 10 帧清洁才标记完成）
- 抖动抑制（避免单帧误检）

#### 模式 2: 累计计数（Accumulated Count）

**用途**：累计满足条件的帧数（不要求连续）

**实现**：
```python
def _update_client_state(self, state, result):
    detected = result.result.get("bending", {}).get("bending_detected")

    if detected:
        count = state.increment_counter("bending_count")  # 只递增，不重置
        if count >= 5:  # 累计 5 次
            state.set_custom("bending_severe", True)
```

**适用场景**：
- 折弯检测（累计 5 次折弯事件）
- 异常统计（累计异常次数）
- 事件计数（不关心连续性）

#### 模式 3: 滑动窗口（Sliding Window）

**用途**：统计最近 N 帧中满足条件的比例

**实现**：
```python
def _update_client_state(self, state, result):
    detected = result.result.get("bubble", {}).get("bubble_detected")

    # 更新历史窗口
    state.push_to_history("bubble_window", detected, max_len=10)

    # 滑动窗口统计
    history = state.get_history("bubble_window")
    if len(history) >= 10:
        ratio = sum(history) / len(history)
        if ratio >= 0.7:  # 70% 的帧检测到
            state.mark_step_completed()
```

**适用场景**：
- 稳定性判断（最近 10 帧中 7 帧有气泡）
- 噪声过滤（避免单帧误检影响判断）
- 置信度统计

### 完整示例

```python
class CustomWorkerService(ModelWorkerService):
    def _update_client_state(self, state: ClientState, result: InferenceResult):
        """自定义时序分析逻辑"""

        if result.stage == "LEAK":
            # 气泡检测：连续帧模式
            bubble_res = result.result.get("bubble", {})
            if bubble_res.get("bubble_detected"):
                count = state.increment_counter("continuous_bubble")
                if count >= 3:
                    state.mark_step_completed()
                    print(f"[{result.client_id}] 气泡检测完成（连续 {count} 帧）")
            else:
                state.reset_counter("continuous_bubble")

            # 折弯检测：累计计数模式
            bending_res = result.result.get("bending", {})
            if bending_res.get("bending_detected"):
                count = state.increment_counter("bending_count")
                if count >= 5:
                    state.set_custom("bending_severe", True)

        elif result.stage == "CLEAN":
            # 清洁度检测：滑动窗口模式
            quality_res = result.result.get("quality", {})
            is_clean = quality_res.get("is_clean", False)

            # 更新历史窗口
            state.push_to_history("clean_window", is_clean, max_len=10)

            # 滑动窗口统计
            history = state.get_history("clean_window")
            if len(history) >= 10:
                clean_ratio = sum(history) / len(history)
                if clean_ratio >= 0.8:  # 80% 的帧清洁
                    state.mark_step_completed()
```

### 最佳实践

#### 1. 选择合适的模式

| 需求 | 推荐模式 | 阈值建议 |
|-----|---------|---------|
| 抑制误检 | 连续帧检测 | 3-5 帧（0.3-0.5秒） |
| 事件累计 | 累计计数 | 依业务定 |
| 稳定性判断 | 滑动窗口 | 70-80% 比例 |

#### 2. 根据推理帧率调整阈值

```python
# 假设推理帧率为 10fps
inference_fps = 10

# 连续帧阈值：约 0.3-0.5 秒
consecutive_threshold = int(inference_fps * 0.3)  # 3 帧

# 滑动窗口长度：约 1-2 秒
window_size = inference_fps * 1  # 10 帧
```

#### 3. 处理边界情况

```python
def _update_client_state(self, state, result):
    # 切换 stage 时重置时序状态
    current_stage = state.get_stage()
    if current_stage != result.stage:
        state.set_stage(result.stage)
        # 重置所有时序状态
        state.reset_counter("continuous_bubble")
        state.reset_counter("bending_count")
        state.clear_history("bubble_window")

    # 正常时序逻辑
    # ...
```

#### 4. 监控时序状态

```python
# 获取客户端时序状态快照
snapshot = state.to_dict()
print(f"时序计数器: {snapshot['sequence_counters']}")
# 输出: {"continuous_bubble": 5, "bending_count": 3}
```

### 优势

相比原有的 `pipeline_base.py` 设计：

| 对比项 | 旧设计 (pipeline_base) | 新设计 (inference 模块) |
|-------|----------------------|----------------------|
| 时序逻辑位置 | 分散在各 SubtaskPipeline | 集中在 InferWorker |
| 状态管理 | 各 Pipeline 独立维护 | ClientState 统一管理 |
| 跨任务时序 | 难以实现 | 天然支持 |
| 自定义规则 | 需要修改 Pipeline | 继承 ModelWorkerService |
| 线程安全 | 需要手动保证 | ClientState 内置锁 |
| 代码复杂度 | 较高（分散） | 较低（集中） |

---

## 扩展指南

### 自定义状态更新逻辑

继承 `ModelWorkerService` 并覆写 `_update_client_state` 方法：

```python
from app.services.inference import ModelWorkerService

class CustomModelWorkerService(ModelWorkerService):
    def _update_client_state(self, state, result):
        if result.stage == "LEAK":
            bubble_res = result.result.get("bubble", {})
            if bubble_res.get("bubble_detected"):
                count = state.increment_counter("continuous_bubble")
                if count >= 5:  # 自定义阈值
                    state.mark_step_completed()
            else:
                state.reset_counter("continuous_bubble")
```

### 自定义可视化逻辑

```python
class CustomModelWorkerService(ModelWorkerService):
    def _visualize_result(self, result, cq):
        raw_frame_data = cq.get_latest_raw_frame()
        if raw_frame_data is None:
            return None

        frame, timestamp = raw_frame_data
        annotated = frame.copy()

        # 在帧上绘制检测结果
        for subtask_name, subtask_res in result.result.items():
            # 调用对应 task 的 visualize 方法
            # annotated = task.visualize(annotated, subtask_res)
            pass

        return annotated
```

### 添加新的 Dispatcher 策略

继承 `StageAwareDispatcher` 并覆写调度逻辑：

```python
from app.services.inference.dispatcher import StageAwareDispatcher

class PriorityDispatcher(StageAwareDispatcher):
    def _fetch_and_dispatch_round(self):
        # 实现优先级调度逻辑
        # 例如：高优先级客户端优先取帧
        pass
```

---

## 动态客户端管理

### 客户端生命周期

**重要**: 客户端是动态创建和清理的，推理服务必须能够适应客户端的动态变化。

#### 客户端创建时机
- 新的 RTSP 流连接建立时
- WebSocket 客户端连接时
- API 请求创建新任务时

#### 客户端清理时机
- RTSP 流断开连接
- WebSocket 客户端断开
- 任务完成或超时
- 用户主动停止任务

### 自动同步机制

推理服务通过以下方式处理动态客户端：

#### 1. 使用 ClientManager 自动获取

```python
# 推荐方式：从 ClientManager 自动获取客户端列表
service = create_model_worker_service_from_manager()
```

**优点**：
- 初始化时自动获取所有现有客户端
- 通过 `refresh_client_queues()` 可以同步最新客户端列表

#### 2. Dispatcher 容错设计

Dispatcher 在轮询时会自动处理客户端变化：

```python
def _fetch_and_dispatch_round(self):
    # 使用 list() 创建快照，避免迭代时字典被修改
    for client_id, cq in list(self.client_queues_map.items()):
        if not cq.ca_ready:
            continue
        # ... 处理逻辑
```

#### 3. 结果回写时的安全检查

```python
def _write_back_results(self, results):
    for res in results:
        cq = self.client_queues_map.get(res.client_id)
        if cq is None:
            # 客户端已被清理，跳过
            continue
        # ... 回写逻辑
```

### 推荐使用模式

#### 方案 A: 定期刷新（推荐）

```python
import threading
from app.services.inference import create_model_worker_service_from_manager

# 创建服务
service = create_model_worker_service_from_manager()
service.start()

# 定期刷新客户端列表（例如每 5 秒）
def refresh_loop():
    while not stop_event.is_set():
        service.refresh_client_queues()
        time.sleep(5)

stop_event = threading.Event()
refresh_thread = threading.Thread(target=refresh_loop, daemon=True)
refresh_thread.start()
```

#### 方案 B: 事件驱动刷新

```python
from app.services.client_manager import client_manager

# 在客户端添加/移除时主动刷新
def on_client_added(client_id: str):
    """客户端添加回调"""
    service.refresh_client_queues()
    print(f"[Inference] 已添加客户端: {client_id}")

def on_client_removed(client_id: str):
    """客户端移除回调"""
    service.refresh_client_queues()
    print(f"[Inference] 已移除客户端: {client_id}")

# 注册回调（需要在 ClientManager 中实现回调机制）
# client_manager.on_client_added = on_client_added
# client_manager.on_client_removed = on_client_removed
```

#### 方案 C: 无需刷新（未来优化）

**理想情况**：Dispatcher 和 Service 直接引用 ClientManager 的动态字典：

```python
# 当前实现
self.client_queues_map = self._client_manager.get_all_clients()  # 快照

# 未来优化
self.client_queues_map = self._client_manager._clients  # 直接引用
```

**优点**：无需 `refresh_client_queues()`，自动同步
**缺点**：需要确保线程安全

### 客户端动态变化的影响

| 场景 | 影响 | 处理方式 |
|-----|------|---------|
| 新客户端加入 | Dispatcher 不会轮询新客户端 | 调用 `refresh_client_queues()` |
| 客户端离开 | 队列中可能还有待推理帧 | 安全检查：`if cq is None: continue` |
| 推理过程中离开 | 结果回写失败 | 安全检查：跳过已清理的客户端 |
| 大量客户端频繁变化 | 刷新开销增加 | 使用定期刷新策略（5-10s） |

---

## 常见问题

### Q: 客户端动态添加后推理服务无响应？

**原因**：推理服务初始化时获取的是客户端列表的快照，新添加的客户端不在列表中。

**解决方案**：
```python
# 1. 手动刷新
service.refresh_client_queues()

# 2. 或者使用定期刷新
def refresh_loop():
    while True:
        service.refresh_client_queues()
        time.sleep(5)

threading.Thread(target=refresh_loop, daemon=True).start()
```

### Q: 客户端清理后是否需要特殊处理？

**不需要**。推理服务已经实现了安全检查：
- Dispatcher 轮询时跳过不存在的客户端
- 结果回写时检查客户端是否存在
- 不会因为客户端清理而崩溃

### Q: 如何切换客户端的 stage？

```python
from app.services.client_manager import client_manager

cq = client_manager.get_client("client_1")
cq.state.set_stage("CLEAN")  # 自动重置步骤状态和计数器
```

### Q: 如何判断步骤是否完成？

```python
if cq.state.is_step_completed():
    # 步骤完成逻辑
    cq.state.set_stage("CLEAN")
```

### Q: CUDA Stream 并行效果不明显？

**可能原因**：
1. 模型太大，GPU 已满载
2. 模型太小，并行开销 > 收益
3. 只有 1 个模型，无并行空间

**建议**：
- 使用 2-3 个轻量级模型（YOLOv8n/s）
- GPU 显存占用 < 80%
- 实测对比开启/关闭 CUDA Stream

### Q: 如何减少推理延迟？

1. 降低 batch_size（牺牲吞吐换延迟）
2. 减少调度间隔 `fetch_interval`
3. 使用更快的模型（YOLOv8n）
4. 降低推理帧率 `inference_fps`

---

## 性能基准

### 测试环境

- GPU: RTX 4090 (24GB)
- CPU: 64-core
- RAM: 32GB
- 模型: YOLOv8n (2 个并行)
- 输入: 640×480 RGB 图像

### 性能指标

| 配置 | Batch Size | CUDA Stream | 吞吐量 (fps) | 延迟 (ms) |
|-----|-----------|-------------|-------------|----------|
| 顺序推理 | 1 | ❌ | 45 | 22 |
| 顺序推理 | 4 | ❌ | 120 | 33 |
| 并行推理 | 4 | ✅ | 150 | 27 |
| 顺序推理 | 8 | ❌ | 180 | 44 |
| 并行推理 | 8 | ✅ | 220 | 36 |

**结论**：
- CUDA Stream 并行提升 20-25%
- Batch Size 从 1→4：吞吐提升 2.7x
- Batch Size 从 4→8：吞吐提升 1.5x（边际递减）

---

## 向后兼容

旧的导入方式仍然支持，但会收到 DeprecationWarning：

```python
# 旧的导入方式（不推荐）
from app.services.model_worker_pool import ModelWorkerService

# DeprecationWarning: app.services.model_worker_pool 已被弃用，
# 请使用 app.services.inference 代替
```

**迁移指南**：

```python
# 替换导入
- from app.services.model_worker_pool import ModelWorkerService
+ from app.services.inference import ModelWorkerService

# 其他代码无需修改
service = create_model_worker_service_from_manager()
service.start()
```

---

## 参考资料

- [REFACTORING_PLAN.md](../app/services/inference/REFACTORING_PLAN.md) - 重构计划
- [model_worker_pool_usage.md](../app/services/model_worker_pool_usage.md) - 使用指南
- [PIPELINE_BASE.md](./PIPELINE_BASE.md) - Pipeline 基类设计
- [AI_INFERENCE_ARCHITECTURE.md](./AI_INFERENCE_ARCHITECTURE.md) - AI 推理架构

---

## 更新日志

| 日期 | 版本 | 说明 |
|-----|------|------|
| 2026-01-21 | v2.0.0 | 模块化重构，拆分为 6 个文件 |
| 2025-12-XX | v1.0.0 | 初始版本（单文件 600+ 行）|
