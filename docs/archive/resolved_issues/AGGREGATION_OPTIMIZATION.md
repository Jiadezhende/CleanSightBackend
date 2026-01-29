# 聚合逻辑优化：解耦推理速率与可视化帧产出速率

## 优化背景

由于推理帧率降低（从30fps降至10-15fps），原有的同步可视化机制导致 `processed_frame` 的产出速率也随之降低，影响了实时展示的流畅度。

## 核心问题

- **旧方案**：推理线程在 `infer_frame` 时缓存原始帧，异步聚合线程使用缓存的帧进行可视化
- **问题**：可视化帧产出速率 = 推理速率，推理降频后可视化也跟着降频

## 优化方案

### 1. ClientQueues 维护最新原始帧

**修改文件**: `app/services/client.py`

**新增属性**：
```python
# 最新原始帧缓存（用于异步聚合可视化）
self.latest_raw_frame: Optional[np.ndarray] = None
self.latest_raw_timestamp: float = 0.0
```

**关键方法**：
- `append_ca_raw()`: 每次添加原始帧时自动更新 `latest_raw_frame`
- `get_latest_raw_frame()`: 线程安全地获取最新原始帧副本

**优势**：
- 拉流线程以30fps更新最新帧
- 推理线程按降频速率（10-15fps）消费
- 异步聚合线程始终能获取最新的30fps原始帧

### 2. TaskPipeline 关联 ClientQueues

**修改文件**：
- `app/services/pipeline_base.py`
- `app/services/ai.py`

**新增方法**：
```python
class TaskPipelineBase:
    def set_client_queues(self, client_queues: Any) -> None:
        """设置 ClientQueues 实例，用于异步聚合时获取最新原始帧"""
        self._client_queues = client_queues
    
    def get_client_queues(self) -> Optional[Any]:
        """获取 ClientQueues 实例"""
        return self._client_queues
```

**集成点** (`ai.py`):
```python
# 在批量推理前设置 ClientQueues 引用
pipeline = self._get_or_create_leak_pipeline(client_id, task)
if client_id:
    client_queues = self._get_or_create_client(str(client_id))
    pipeline.set_client_queues(client_queues)
```

### 3. 异步聚合逻辑优化

**修改文件**: `app/services/task_pipeline/leak/leak_test.py`

**旧逻辑**：
```python
# 在 infer_frame 时缓存原始帧
with self._frame_lock:
    self._latest_frame = frame.copy()

# 异步聚合时使用缓存的帧
with self._frame_lock:
    base = self._latest_frame.copy()
```

**新逻辑**：
```python
def _visualize_and_update_state(...):
    # 1. 从 ClientQueues 获取最新原始帧（30fps）
    client_queues = self.get_client_queues()
    raw_frame_data = client_queues.get_latest_raw_frame()
    base_frame, frame_timestamp = raw_frame_data
    
    # 2. 如果有新的推理结果，使用新结果；否则沿用上一个推理结果
    if not subtask_results:
        # 沿用上一个推理结果，但更新时间戳
        with self._result_lock:
            last_result = self._last_inference_result.copy()
            last_result["timestamp"] = frame_timestamp
            subtask_results = last_result.get("subtasks", {})
    
    # 3. 在最新原始帧上进行可视化
    annotated = base_frame
    # ... 执行可视化 ...
    
    # 4. 写入 frame cache
    fd = FrameData(timestamp=frame_timestamp, frame=annotated, ...)
    self.rt_cache_frame.append(fd)
```

**关键改进**：
- ❌ 移除 `_latest_frame` 和 `_frame_lock`（不再在推理线程中缓存）
- ✅ 添加 `_last_inference_result` 和 `_result_lock`（缓存推理结果）
- ✅ 从 `ClientQueues` 获取最新30fps原始帧
- ✅ 沿用上一个推理结果（时间戳更新）保持可视化连续性

## 效果对比

| 指标 | 优化前 | 优化后 |
|-----|-------|-------|
| 拉流速率 | 30fps | 30fps |
| 推理速率 | 30fps → 10fps | 10fps |
| 可视化帧产出 | 10fps | **30fps** ✅ |
| 推理结果更新 | 10fps | 10fps |
| 可视化连续性 | 低频跳跃 | **流畅连续** ✅ |

## 数据流示意图

```
拉流线程 (30fps)
    │
    ├──> CA-Raw-Queue (30fps 原始帧)
    │       │
    │       └──> latest_raw_frame 更新
    │
    └──> CA-Ready-Queue (10fps 推理帧)
             │
             ↓
       推理线程 (10fps)
             │
             ├──> 执行推理 (每3帧推理1次)
             │
             └──> _last_inference_result 更新
                      │
                      ↓
       异步聚合线程 (30fps 定时触发)
             │
             ├──> 从 ClientQueues 获取 latest_raw_frame (30fps)
             │
             ├──> 检查是否有新推理结果
             │    ├─ 有新结果 → 使用新结果
             │    └─ 无新结果 → 沿用上一个结果 (时间戳更新)
             │
             └──> 可视化 + 写入 processed_frame (30fps)
```

## 验证测试

运行测试脚本验证优化：
```bash
python test_aggregation_optimization.py
```

测试覆盖：
1. ✅ ClientQueues 维护最新原始帧
2. ✅ TaskPipeline 可以从 ClientQueues 获取原始帧
3. ✅ 没有新推理结果时沿用上一个结果

## 实际运行效果

启动服务后，预期日志输出：

```
[StreamService] 拉流速率: 30fps, 写入 CA-Raw-Queue
[StreamService] 降频写入 CA-Ready-Queue: 10fps (每3帧写入1次)
[AI] Using ClientManager queue for client 172.16.77.220
[AI] TaskPipeline 批量推理 for client 172.16.77.220: 批次大小=10
[Pipeline] 异步聚合线程: 从 ClientQueues 获取最新原始帧
[Pipeline] 写入 processed_frame: 30fps 可视化帧产出
```

## 后续优化空间

1. **自适应聚合频率**：根据推理速率动态调整聚合频率
2. **结果插值**：在两次推理之间进行结果平滑插值
3. **多级缓存**：为不同优先级任务设置不同的帧缓存策略

## 相关文件清单

```
app/services/client.py                    # ClientQueues 最新帧缓存
app/services/pipeline_base.py             # TaskPipelineBase ClientQueues 关联
app/services/ai.py                        # InferenceManager 集成点
app/services/task_pipeline/leak/leak_test.py  # LeakBubblePipelineService 异步聚合
test_aggregation_optimization.py          # 验证测试脚本
```
