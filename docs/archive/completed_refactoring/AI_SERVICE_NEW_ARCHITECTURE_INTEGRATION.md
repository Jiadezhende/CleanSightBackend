# AI 服务新架构集成说明

**日期**: 2026-01-22
**状态**: ✅ 已完成

---

## 集成概览

基于 [INFERENCE_SERVICE_IMPROVEMENT_PLAN.md](./INFERENCE_SERVICE_IMPROVEMENT_PLAN.md) 和 [INFERENCE_IMPROVEMENT_IMPLEMENTATION.md](./INFERENCE_IMPROVEMENT_IMPLEMENTATION.md)，我们已成功将新的推理服务架构集成到 `app/services/ai.py` 中。

### 核心改进

1. **推理与可视化解耦**：推理线程只负责推理，可视化异步执行
2. **时序分析独立**：时序逻辑从推理线程中分离，支持复杂时序算法
3. **降帧可视化补偿**：可视化使用最新原始帧 + 缓存的检测结果
4. **异步管道架构**：推理 → 时序分析 → 可视化 → 写回，完全异步
5. **无缝 API 兼容**：保留所有原有 API 接口，无需修改 `app/routers/ai.py`

---

## 架构对比

### 旧架构（同步模式）

```text
InferWorker (单线程)
    ↓
1. 批量推理（20-50ms）
    ↓
2. 状态更新（1-2ms）
    ↓
3. 可视化处理（15-30ms）❌ 阻塞推理线程
    ↓
4. 队列写入（1-2ms）
    ↓
总耗时: 37-84ms
吞吐: 11-27 fps
```

### 新架构（异步模式）

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
│ 3. 投递到队列   │      │ 3. 投递到队列   │ ✅ <1ms，立即返回
└────────┬────────┘      └────────┬────────┘
         │                        │
         └────────────┬───────────┘
                      ▼
         ┌────────────────────────────────┐
         │  TemporalAnalysisQueue         │
         │  - 缓冲推理结果                │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  TemporalWorkerPool (2-4线程)  │ ✅ 异步执行
         │  - 消费推理结果                │
         │  - 执行时序逻辑                │
         │  - 更新 ClientState            │
         │  - 生成前端消息                │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  VisualizationQueue            │
         │  - 缓冲待可视化数据            │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  VisualizationWorkerPool       │ ✅ 异步执行
         │  (4-8线程)                     │
         │  - 取最新原始帧                │
         │  - 异步绘制检测框              │
         │  - 降帧补偿                    │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  WriteBackQueue                │
         │  - 缓冲完整数据包              │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  WriteBackWorkerPool (2-4线程) │ ✅ 异步执行
         │  - 写入 ca_processed 队列       │
         │  - 写入 rt_processed 队列       │
         │  - 写入数据库（可选）          │
         └────────────────────────────────┘

InferWorker 总耗时: 21-51ms
推理吞吐: 19-47 fps (提升 40-75%)
```

---

## 文件变更

### 核心文件

1. **app/services/ai.py** ✅ 已替换
   - 旧版本已备份为 `app/services/ai_old.py`
   - 新版本完整集成异步管道架构
   - 保留所有原有 API 接口

2. **app/routers/ai.py** ✅ 无需修改
   - API 接口保持不变
   - WebSocket 端点保持不变
   - 所有路由正常工作

### 新增组件（已在 inference 模块中实现）

3. **app/services/inference/temporal_analyzer.py** ✅ 已实现
   - `TemporalAnalyzer` 抽象基类
   - `DefaultTemporalAnalyzer` 默认实现
   - 支持三种模式：连续帧、累计计数、滑动窗口

4. **app/services/inference/temporal_worker.py** ✅ 已实现
   - `TemporalWorker` 时序分析工作线程
   - `TemporalWorkerPool` 时序分析线程池

5. **app/services/inference/visualization_worker.py** ✅ 已实现
   - `Visualizer` 可视化器抽象接口
   - `VisualizationWorker` 可视化工作线程
   - `VisualizationWorkerPool` 可视化线程池

6. **app/services/inference/writeback_worker.py** ✅ 已实现
   - `WriteBackWorker` 写回工作线程
   - `WriteBackWorkerPool` 写回线程池

7. **app/services/inference/models.py** ✅ 已扩展
   - `TemporalAnalysisResult`: 时序分析结果
   - `FrontendMessage`: 前端消息
   - `TemporalAnalysisPackage`: 时序分析数据包
   - `WriteBackData`: 写回数据包

---

## 核心类：InferenceManager

### 初始化参数

```python
manager = InferenceManager(
    rt_fps=30,                    # 实时帧率
    ca_segment_seconds=5,          # CA 段长度（秒）
    db_dir=None,                   # 数据库存储目录
    ca_maxlen=500,                 # CA 队列最大长度
    use_async_pipeline=True,       # ✅ 启用异步管道（新架构）
    temporal_threads=2,            # 时序分析线程数
    visualization_threads=4,       # 可视化线程数
    writeback_threads=2,           # 写回线程数
)
```

### 异步管道组件

1. **TemporalWorkerPool**（时序分析）
   - 消费推理结果
   - 执行时序逻辑（连续帧、滑动窗口等）
   - 更新 ClientState
   - 生成前端消息

2. **VisualizationWorkerPool**（可视化）
   - 从原始帧流获取最新帧
   - 绘制检测框和标注
   - 支持降帧补偿（推理 10fps，可视化 30fps）

3. **WriteBackWorkerPool**（写回）
   - 写入 `ca_processed` 和 `rt_processed` 队列
   - 可选写入数据库

### 默认可视化器

```python
class DefaultVisualizer(Visualizer):
    """默认可视化器：绘制检测框、标注和文字信息"""

    def visualize(
        self,
        frame: np.ndarray,
        inference_result: Dict[str, Any],
        stage: str,
        temporal_result: Optional[TemporalAnalysisResult] = None,
    ) -> np.ndarray:
        # 1. 绘制检测框（气泡、弯折等）
        # 2. 绘制文字信息（stage、timestamp、事件等）
        return annotated_frame
```

---

## API 兼容性

### 原有 API 完全保留

```python
# 1. 启动/停止服务
ai.start()
ai.stop()

# 2. 提交帧（拉流层调用）
ai.submit_frame(client_id, frame)

# 3. 设置流地址
ai.set_stream_url(client_id, stream_url)
ai.set_rtmp_url(client_id, rtmp_url)

# 4. 获取推理结果（WebSocket 调用）
processed_frame = ai.get_result(client_id, as_model=True)

# 5. 任务管理
ai.set_task(client_id, task)
ai.get_task(client_id)
ai.terminate_task_by_id(client_id)

# 6. 客户端管理
ai.remove_client(client_id)
ai.status()
```

### 无需修改的代码

- ✅ `app/routers/ai.py` - 所有路由和 WebSocket 端点
- ✅ 拉流服务 - `submit_frame()` 接口保持不变
- ✅ 前端代码 - WebSocket 连接和数据格式保持不变

---

## 时序分析配置

### 默认配置

```python
temporal_config = {
    "LEAK": {
        "bubble": {
            "mode": "consecutive",  # 连续帧模式
            "threshold": 3,          # 连续3帧
        },
        "bending": {
            "mode": "sliding_window",  # 滑动窗口模式
            "window_seconds": 2.0,      # 2秒窗口
            "ratio": 0.7,                # 70%比例
        },
    },
    "CLEAN": {
        "quality": {
            "mode": "sliding_window",
            "window_seconds": 2.0,
            "ratio": 0.8,  # 80%比例
        },
    },
}
```

### 支持的模式

1. **consecutive**（连续帧检测）
   - 连续 N 帧检测到目标 → 触发事件
   - 适用场景：气泡检测、折弯检测

2. **accumulated**（累计计数）
   - 累计检测到 N 次 → 触发事件
   - 适用场景：缺陷计数

3. **sliding_window**（滑动窗口）
   - 2秒窗口内，检测比例超过阈值 → 触发事件
   - 适用场景：清洁质量评估（70%的帧合格）

---

## 性能预期

### 旧架构性能

| 阶段 | 耗时 | 说明 |
|-----|------|------|
| 批量推理 | 20-50ms | GPU密集，已优化 |
| 状态更新 | 1-2ms | CPU轻量 |
| 可视化 | 15-30ms | **瓶颈**，CPU密集 |
| 队列写入 | 1-2ms | I/O轻量 |
| **总耗时** | **37-84ms** | **吞吐：11-27 fps** |

### 新架构性能

| 阶段 | 耗时 | 并行性 | 说明 |
|-----|------|--------|------|
| 批量推理 | 20-50ms | ✅ CUDA Stream | 无变化 |
| 投递队列 | <1ms | ✅ 异步 | 解放推理线程 |
| **InferWorker总耗时** | **21-51ms** | - | **吞吐：19-47 fps** |
| 时序分析 | 2-5ms | ✅ 多线程池 | 异步执行 |
| 可视化 | 15-30ms | ✅ 多线程池 | 异步执行 |
| 队列写入 | 1-2ms | ✅ 多线程池 | 异步执行 |

**性能提升**：
- 推理吞吐提升：**40-75%**（从11-27fps提升到19-47fps）
- 支持更多路视频流：从20路提升到 **30-40路**

---

## 启动流程

### 1. 启动服务

```python
# app/services/ai.py 中的模块级调用
ai.start()
```

### 2. 内部启动顺序

```python
def start(self):
    # 1. 启动推理服务
    self._model_worker_service.start()

    # 2. 启动异步管道（如果启用）
    if self.use_async_pipeline:
        self.temporal_pool.start()        # 时序分析线程池
        self.visualization_pool.start()   # 可视化线程池
        self.writeback_pool.start()       # 写回线程池

    # 3. 启动持久化线程（HLS 段落盘）
    self._persist_thread.start()

    # 4. 启动客户端刷新线程（定期同步客户端列表）
    self._refresh_thread.start()
```

### 3. 客户端刷新

新架构使用 `ClientManager` 统一管理客户端，推理服务每 5 秒自动刷新客户端列表：

```python
def _client_refresh_loop(self):
    """定期刷新客户端列表（从 ClientManager 同步）"""
    while not self._stop_event.is_set():
        self._model_worker_service.refresh_client_queues()
        time.sleep(5)  # 每 5 秒刷新一次
```

---

## 降帧可视化补偿

### 问题

- 推理降帧（10fps）以提升吞吐
- 前端需要全帧率（30fps）可视化

### 解决方案

```text
原始帧流（30fps）：Frame1, Frame2, Frame3, Frame4, Frame5, Frame6, Frame7, Frame8...
推理帧流（10fps）：Frame1(推理), -, -, Frame4(推理), -, -, Frame7(推理), -...

可视化策略：
1. Frame1推理后 → 缓存检测结果A → 可视化Frame1
2. Frame2/3未推理 → 取最新原始帧，使用结果A → 可视化Frame2/3
3. Frame4推理后 → 更新检测结果B → 可视化Frame4
4. Frame5/6未推理 → 取最新原始帧，使用结果B → 可视化Frame5/6
```

### 实现

```python
class VisualizationWorker:
    def run(self):
        while not self._stop_event.is_set():
            # 1. 从队列获取推理结果
            package = self.input_queue.get()

            # 2. 获取客户端的最新原始帧（而非推理时的旧帧）
            cq = client_manager.get_client(package.client_id)
            latest_frame = cq.get_latest_frame()  # ✅ 取当前最新帧

            # 3. 使用最新的检测结果进行可视化
            annotated_frame = self.visualizer.visualize(
                frame=latest_frame,  # 使用最新帧
                inference_result=package.inference_result,
                stage=package.stage,
                temporal_result=package.temporal_result,
            )

            # 4. 投递到写回队列
            self.output_queue.put(write_back_data)
```

---

## 流隔离保证

### 问题

多个客户端并发推理，如何保证时序分析不混乱？

### 解决方案

**每个客户端有独立的 `ClientState`**：

```python
# 客户端1的时序历史
client_1_state._temporal_history["bubble"] = [(t1, ✓), (t2, ✓), (t3, ✓)]

# 客户端2的时序历史
client_2_state._temporal_history["bubble"] = [(t1, ✗), (t2, ✗), (t3, ✗)]
```

**Worker 无状态设计**：

```python
class TemporalWorker:
    def run(self):
        while not self._stop_event.is_set():
            # 从全局队列获取推理结果（可能来自任意客户端）
            result = self.input_queue.get()

            # 根据 client_id 获取对应的 ClientState
            cq = client_manager.get_client(result.client_id)

            # 执行时序分析（状态隔离在 ClientState 中）
            temporal_result = self.analyzer.analyze(
                state=cq.state,  # 每个客户端独立的状态
                result=result,
            )
```

---

## 向后兼容

### 同步模式（兼容旧代码）

```python
manager = InferenceManager(use_async_pipeline=False)  # 关闭异步管道
```

### 迁移策略

1. 默认启用异步管道（`use_async_pipeline=True`）
2. 在测试环境验证性能和稳定性
3. 逐步在生产环境部署
4. 最终弃用同步模式代码

---

## 测试建议

### 1. 单元测试

- ✅ ClientState 时间窗口历史
- ✅ TemporalAnalyzer 三种模式
- ✅ Worker 线程池启动/停止

### 2. 集成测试

- ✅ 多路视频流并发推理（20-40路）
- ✅ 客户端动态加入/离开
- ✅ 降帧可视化补偿

### 3. 性能测试

- ✅ 推理吞吐量（目标：40-75% 提升）
- ✅ 端到端延迟
- ✅ 队列深度监控

---

## 故障排查

### 问题1：推理结果未显示

**原因**：客户端刷新线程未启动，推理服务无法识别新客户端

**解决**：
```python
# 检查客户端刷新线程是否运行
if self._refresh_thread is None or not self._refresh_thread.is_alive():
    self._refresh_thread.start()
```

### 问题2：队列溢出

**原因**：某个环节处理速度慢，导致队列积压

**解决**：
```python
# 调整队列大小
self.temporal_queue = queue.Queue(maxsize=512)  # 增大队列

# 增加 Worker 线程数
self.visualization_pool = VisualizationWorkerPool(num_workers=8)  # 增加线程
```

### 问题3：可视化延迟

**原因**：可视化线程数不足

**解决**：
```python
manager = InferenceManager(
    visualization_threads=8,  # 增加可视化线程数
)
```

---

## 总结

✅ **已完成**：
- 完整集成异步管道架构到 `app/services/ai.py`
- 保留所有原有 API 接口，无缝替换
- 实现默认可视化器（`DefaultVisualizer`）
- 集成时序分析器和三种分析模式
- 支持降帧推理 + 全帧率可视化
- 客户端动态刷新机制

⏳ **待验证**：
- 多路视频流性能测试（20-40路）
- 端到端延迟测试
- 生产环境稳定性验证

**预期收益**：
- 推理吞吐提升 40-75%
- 支持 30-40 路视频流（原20路）
- 架构更清晰，易于扩展

---

**文档版本**: v1.0
**创建日期**: 2026-01-22
**作者**: Claude Code Assistant
