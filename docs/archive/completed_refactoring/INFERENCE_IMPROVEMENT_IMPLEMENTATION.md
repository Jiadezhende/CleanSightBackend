# 推理服务架构改造实施总结

**日期**: 2026-01-22
**状态**: 核心组件已完成，待集成到 ModelWorkerService

---

## 改造概览

基于 [INFERENCE_SERVICE_IMPROVEMENT_PLAN.md](./INFERENCE_SERVICE_IMPROVEMENT_PLAN.md) 的设计，我们已完成核心组件的实现。

### 核心改进

1. **推理与可视化解耦**：推理线程只负责推理，可视化异步执行
2. **时序分析独立**：时序逻辑从推理线程中分离，支持复杂时序算法
3. **降帧可视化补偿**：可视化使用最新原始帧 + 缓存的检测结果
4. **异步管道架构**：推理 → 时序分析 → 可视化 → 写回，完全异步

---

## 已完成的组件

### 1. 数据结构扩展

#### 1.1 ClientState 时间窗口支持 ✅

**文件**: [app/services/client.py](../app/services/client.py)

**新增方法**:
- `push_temporal_history()`: 追加时序历史，自动清理过期数据
- `get_temporal_history()`: 获取窗口内的历史数据（带时间戳）
- `get_temporal_values()`: 获取窗口内的历史值（不含时间戳）
- `clear_temporal_history()`: 清空时序历史
- `set_history_window()`: 设置历史窗口大小

**特性**:
- 支持2秒时间窗口的滑动窗口分析
- 自动清理超过窗口的过期数据
- 线程安全（RLock保护）

#### 1.2 ClientQueues 新增方法 ✅

**文件**: [app/services/client.py](../app/services/client.py)

**新增方法**:
- `get_latest_frame()`: 获取最新原始帧（用于可视化降帧补偿）

**用途**:
- 支持推理降帧后的可视化补偿
- 可视化使用最新原始帧 + 缓存的检测结果

#### 1.3 新增数据模型 ✅

**文件**: [app/services/inference/models.py](../app/services/inference/models.py)

**新增模型**:

1. **TemporalAnalysisResult**: 时序分析结果
   - `stage_changed`: 是否切换stage
   - `new_stage`: 新的stage
   - `step_completed`: 步骤是否完成
   - `events`: 触发的事件列表
   - `state_snapshot`: ClientState 快照

2. **FrontendMessage**: 前端消息（精简版）
   - `detections`: 检测结果（布尔值）
   - `confidences`: 置信度
   - `status_message`: 状态提示
   - `progress`: 阶段进度

3. **TemporalAnalysisPackage**: 时序分析数据包
   - 传递给可视化线程的完整数据

4. **WriteBackData**: 写回数据包
   - 包含可视化后的帧和所有元数据

---

### 2. 核心组件实现

#### 2.1 TemporalAnalyzer 时序分析器 ✅

**文件**: [app/services/inference/temporal_analyzer.py](../app/services/inference/temporal_analyzer.py)

**支持的模式**:

1. **consecutive (连续帧检测)**:
   - 连续N帧检测到目标 → 触发事件
   - 适用场景：气泡检测、折弯检测

2. **accumulated (累计计数)**:
   - 累计检测到N次 → 触发事件
   - 适用场景：缺陷计数

3. **sliding_window (滑动窗口)**:
   - 2秒窗口内，检测比例超过阈值 → 触发事件
   - 适用场景：清洁质量评估（70%的帧合格）

**配置示例**:
```python
config = {
    "LEAK": {
        "bubble": {
            "mode": "consecutive",
            "threshold": 3,  # 连续3帧
        },
        "bending": {
            "mode": "sliding_window",
            "window_seconds": 2.0,  # 2秒窗口
            "ratio": 0.7,  # 70%比例
        },
    },
}
```

#### 2.2 TemporalWorkerPool 时序分析线程池 ✅

**文件**: [app/services/inference/temporal_worker.py](../app/services/inference/temporal_worker.py)

**职责**:
- 消费推理结果
- 执行时序分析逻辑
- 更新 ClientState
- 生成前端消息
- 投递到可视化队列

**线程数**: 2-4个（CPU密集型）

**特性**:
- 流隔离：每个客户端的状态独立
- 无状态Worker：Worker不维护状态，通过 `client_id` 路由到 `ClientState`
- 自动负载均衡：多个Worker竞争获取任务

#### 2.3 VisualizationWorkerPool 可视化线程池 ✅

**文件**: [app/services/inference/visualization_worker.py](../app/services/inference/visualization_worker.py)

**职责**:
- 消费时序分析后的数据包
- **取当前客户端的最新帧**（降帧补偿）
- 绘制检测框、标注、文字信息
- 投递到写回队列

**线程数**: 4-8个（CPU密集型，可并行）

**降帧补偿原理**:
```text
原始帧流（30fps）：Frame1, Frame2, Frame3, Frame4, Frame5, Frame6...
推理帧流（10fps）：Frame1(推理), -, -, Frame4(推理), -, -...

可视化策略：
1. Frame1推理后 → 缓存检测结果A → 可视化Frame1
2. Frame2/3未推理 → 取最新原始帧，使用结果A → 可视化Frame2/3
3. Frame4推理后 → 更新检测结果B → 可视化Frame4
```

**关键方法**:
- `run()`: 主工作循环，从队列获取数据包并可视化
- `visualize_with_cached_result()`: 使用缓存结果可视化未推理的中间帧

#### 2.4 WriteBackWorkerPool 写回线程池 ✅

**文件**: [app/services/inference/writeback_worker.py](../app/services/inference/writeback_worker.py)

**职责**:
- 消费完整数据包
- 写入 ClientQueues（ca_processed、rt_processed）
- 写入数据库（可选）

**线程数**: 2-4个（I/O密集型）

**容错设计**:
- 安全检查客户端是否存在
- 客户端清理后自动跳过

---

## 架构数据流

```text
┌──────────────────────────────────────────────────────┐
│          StageAwareDispatcher (调度器)                │
│          - Round-Robin 轮询所有客户端                 │
│          - 按 Stage 分组（LEAK/CLEAN）                │
└────────────────────┬─────────────────────────────────┘
                     │
         ┌───────────┴──────────┐
         ▼                      ▼
┌─────────────────┐      ┌─────────────────┐
│ InferWorker     │      │ InferWorker     │
│ (LEAK Stage)    │      │ (CLEAN Stage)   │
│ 1. 取batch      │      │ 1. 取batch      │
│ 2. 批量推理     │      │ 2. 批量推理     │
│ 3. 输出结果     │      │ 3. 输出结果     │
└────────┬────────┘      └────────┬────────┘
         │                        │
         └────────────┬───────────┘
                      ▼
         ┌────────────────────────────────┐
         │  TemporalAnalysisQueue         │  ✅ 已实现
         │  - 缓冲推理结果                │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  TemporalWorkerPool (2-4线程)  │  ✅ 已实现
         │  - 消费推理结果                │
         │  - 执行时序逻辑                │
         │  - 更新 ClientState            │
         │  - 生成前端消息                │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  VisualizationQueue            │  ✅ 已实现
         │  - 缓冲待可视化数据            │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  VisualizationWorkerPool       │  ✅ 已实现
         │  (4-8线程)                     │
         │  - 取最新原始帧                │
         │  - 异步绘制检测框              │
         │  - 降帧补偿                    │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  WriteBackQueue                │  ✅ 已实现
         │  - 缓冲完整数据包              │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  WriteBackWorkerPool (2-4线程) │  ✅ 已实现
         │  - 写入 ca_processed 队列       │
         │  - 写入 rt_processed 队列       │
         │  - 写入数据库（可选）          │
         └────────────────────────────────┘
```

---

## 待完成的工作

### 1. 集成到 ModelWorkerService ⏳

**文件**: [app/services/inference/service.py](../app/services/inference/service.py)

**需要的改动**:

1. **初始化异步管道**:
   ```python
   def __init__(self, ..., use_async_pipeline: bool = False):
       if use_async_pipeline:
           # 创建队列
           self.temporal_queue = Queue(maxsize=256)
           self.visualization_queue = Queue(maxsize=256)
           self.writeback_queue = Queue(maxsize=256)

           # 创建时序分析器
           self.temporal_analyzer = DefaultTemporalAnalyzer(temporal_config)

           # 创建 Worker 池
           self.temporal_pool = TemporalWorkerPool(
               input_queue=self.temporal_queue,
               output_queue=self.visualization_queue,
               analyzer=self.temporal_analyzer,
               num_workers=2,
           )

           self.visualization_pool = VisualizationWorkerPool(
               input_queue=self.visualization_queue,
               output_queue=self.writeback_queue,
               visualizer=self.visualizer,
               num_workers=4,
           )

           self.writeback_pool = WriteBackWorkerPool(
               input_queue=self.writeback_queue,
               num_workers=2,
           )
   ```

2. **启动 Worker 池**:
   ```python
   def start(self):
       self.dispatcher.start()

       if self.use_async_pipeline:
           # 启动异步管道
           self.temporal_pool.start()
           self.visualization_pool.start()
           self.writeback_pool.start()

       # 启动推理线程
       for stage in self.worker_pools.keys():
           thread = threading.Thread(target=self._inference_loop, args=(stage,))
           thread.start()
   ```

3. **精简推理循环**:
   ```python
   def _inference_loop(self, stage: str):
       while not self._stop_event.is_set():
           batch = self.dispatcher.get_batch_for_stage(stage)

           # 批量推理
           results = worker_pool.infer_batch(batch)

           if self.use_async_pipeline:
               # 异步模式：投递到时序分析队列
               for result in results:
                   result.frame = batch[i].frame  # 保存原始帧
                   self.temporal_queue.put(result)
           else:
               # 同步模式：直接回写（兼容旧代码）
               self._write_back_results(results)
   ```

### 2. 实现 Visualizer 子类 ⏳

**需要实现的方法**:
- 根据不同的 stage 和 subtask 绘制检测框
- 添加文字标注（stage、timestamp、fps 等）
- 支持不同的可视化风格（debug模式、生产模式）

**参考**:
- `app/services/task_pipeline/leak/leak_test.py` 中的 `_annotate_frame()` 方法

### 3. 添加时序分析器配置 ⏳

**配置示例** (在 `factory.py` 或配置文件中):
```python
temporal_config = {
    "LEAK": {
        "bubble": {
            "mode": "consecutive",
            "threshold": 3,
        },
        "bending": {
            "mode": "sliding_window",
            "window_seconds": 2.0,
            "ratio": 0.7,
        },
    },
    "CLEAN": {
        "quality": {
            "mode": "sliding_window",
            "window_seconds": 2.0,
            "ratio": 0.8,
        },
    },
}
```

---

## 性能预期

### 原架构性能（当前）

| 阶段 | 耗时 | 说明 |
|-----|------|------|
| 批量推理 | 20-50ms | GPU密集，已优化 |
| 状态更新 | 1-2ms | CPU轻量 |
| 可视化 | 15-30ms | **瓶颈**，CPU密集 |
| 队列写入 | 1-2ms | I/O轻量 |
| **总耗时** | **37-84ms** | **吞吐：11-27 fps** |

### 新架构性能（预估）

| 阶段 | 耗时 | 并行性 | 说明 |
|-----|------|--------|------|
| 批量推理 | 20-50ms | ✅ CUDA Stream | 无变化 |
| 投递队列 | <1ms | ✅ 异步 | 解放推理线程 |
| **InferWorker总耗时** | **21-51ms** | - | **吞吐：19-47 fps** |
| 时序分析 | 2-5ms | ✅ 多线程池 | 异步执行 |
| 可视化 | 15-30ms | ✅ 多线程池 | 异步执行 |
| 队列写入 | 1-2ms | ✅ 多线程池 | 异步执行 |

**性能提升**:
- 推理吞吐提升：**40-75%**（从11-27fps提升到19-47fps）
- 支持更多路视频流：从20路提升到 **30-40路**

---

## 使用示例

### 启用异步管道

```python
# app/services/inference/factory.py

from app.services.inference.service import ModelWorkerService
from app.services.inference.temporal_analyzer import DefaultTemporalAnalyzer
from app.services.inference.visualization_worker import Visualizer

# 定义时序分析配置
temporal_config = {
    "LEAK": {
        "bubble": {"mode": "consecutive", "threshold": 3},
        "bending": {"mode": "sliding_window", "window_seconds": 2.0, "ratio": 0.7},
    },
    "CLEAN": {
        "quality": {"mode": "sliding_window", "window_seconds": 2.0, "ratio": 0.8},
    },
}

# 定义可视化器（需要实现）
visualizer = CustomVisualizer()

# 创建服务（启用异步管道）
service = ModelWorkerService(
    stage_configs=stage_configs,
    use_async_pipeline=True,  # ✅ 启用异步管道
    temporal_config=temporal_config,
    visualizer=visualizer,
    temporal_threads=2,
    visualization_threads=4,
    writeback_threads=2,
)

service.start()
```

---

## 向后兼容

**策略**: 使用特性开关（`use_async_pipeline`），逐步迁移

- 默认关闭新架构（`use_async_pipeline=False`）
- 在测试环境开启新架构
- 验证通过后，逐步在生产环境启用
- 最终弃用旧架构代码

---

## 文件清单

### 已修改的文件

1. ✅ [app/services/client.py](../app/services/client.py)
   - 扩展 `ClientState` 支持时间窗口历史
   - 添加 `get_latest_frame()` 方法

2. ✅ [app/services/inference/models.py](../app/services/inference/models.py)
   - 添加 `TemporalAnalysisResult`
   - 添加 `FrontendMessage`
   - 添加 `TemporalAnalysisPackage`
   - 添加 `WriteBackData`

### 新创建的文件

3. ✅ [app/services/inference/temporal_analyzer.py](../app/services/inference/temporal_analyzer.py)
   - `TemporalAnalyzer` 抽象基类
   - `DefaultTemporalAnalyzer` 默认实现

4. ✅ [app/services/inference/temporal_worker.py](../app/services/inference/temporal_worker.py)
   - `TemporalWorker` 时序分析工作线程
   - `TemporalWorkerPool` 时序分析线程池

5. ✅ [app/services/inference/visualization_worker.py](../app/services/inference/visualization_worker.py)
   - `Visualizer` 可视化器抽象接口
   - `VisualizationWorker` 可视化工作线程
   - `VisualizationWorkerPool` 可视化线程池

6. ✅ [app/services/inference/writeback_worker.py](../app/services/inference/writeback_worker.py)
   - `WriteBackWorker` 写回工作线程
   - `WriteBackWorkerPool` 写回线程池

### 待修改的文件

7. ⏳ [app/services/inference/service.py](../app/services/inference/service.py)
   - 集成异步管道架构
   - 添加 `use_async_pipeline` 开关

8. ⏳ [app/services/inference/factory.py](../app/services/inference/factory.py)
   - 添加时序分析器配置
   - 添加可视化器配置

---

## 测试建议

### 单元测试

1. **ClientState 时间窗口**:
   - 测试 `push_temporal_history()` 自动清理过期数据
   - 测试 `get_temporal_history()` 窗口过滤
   - 测试并发访问的线程安全性

2. **TemporalAnalyzer**:
   - 测试连续帧模式（consecutive）
   - 测试累计计数模式（accumulated）
   - 测试滑动窗口模式（sliding_window）

3. **Worker 线程池**:
   - 测试 Worker 的启动和停止
   - 测试队列的消费和生产
   - 测试客户端清理时的容错

### 集成测试

1. **多路视频流**:
   - 测试20-40路视频流的并发推理
   - 监控队列深度，防止溢出

2. **性能基准**:
   - 对比新旧架构的吞吐量和延迟
   - 验证40-75%的性能提升

3. **降帧可视化**:
   - 验证可视化使用最新原始帧
   - 验证检测结果缓存的正确性

---

## 总结

✅ **已完成**:
- ClientState 时间窗口历史支持
- 新增数据模型（4个）
- TemporalAnalyzer 时序分析器
- TemporalWorkerPool 时序分析线程池
- VisualizationWorkerPool 可视化线程池（支持降帧补偿）
- WriteBackWorkerPool 写回线程池

⏳ **待完成**:
- 集成到 ModelWorkerService
- 实现 Visualizer 子类
- 添加时序分析器配置

**预期收益**:
- 推理吞吐提升 40-75%
- 支持 30-40 路视频流（原20路）
- 架构更清晰，易于扩展

---

**文档版本**: v1.0
**创建日期**: 2026-01-22
**作者**: Claude Code Assistant
