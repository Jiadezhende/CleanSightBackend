# 推理服务架构改进方案总结

## 核心改进点

### 1. ⭐ InferWorker 职责拆分

#### 问题

当前 InferWorker 承担过多职责，导致可视化处理（15-30ms）成为瓶颈，阻塞推理线程。

#### 解决方案

将 InferWorker 拆分为四个专门的 Worker Pool：

```text
InferWorker (GPU推理)
    ↓ 异步投递
TemporalWorker (时序分析，CPU)
    ↓ 异步投递
VisualizationWorker (可视化，CPU密集)
    ↓ 异步投递
WriteBackWorker (队列写入，I/O)
```

#### 性能提升

| 指标 | 当前 | 改进后 | 提升 |
|-----|------|--------|------|
| 推理吞吐 | 11-27 fps | 19-47 fps | **40-75%** |
| 支持路数 | 20路 | 30-40路 | **50-100%** |

---

### 2. ⭐ 时序分析独立化

#### 问题

时序分析逻辑与推理耦合，未来复杂算法（如观察2秒窗口）难以集成。

#### 解决方案

创建独立的 `TemporalAnalyzer` 模块，支持多种时序模式：

- **连续帧检测**：连续N帧满足条件（如连续3帧检测到气泡）
- **累计计数**：累计满足条件的次数（如累计5次折弯）
- **滑动窗口**：2秒窗口内X%的帧满足条件（如2秒内70%帧有气泡）
- **时间加权**：近期结果权重更高
- **趋势检测**：检测数量逐渐增加/减少

#### 关键设计

```python
# ClientState 扩展：支持2秒时间窗口
class ClientState:
    def push_temporal_history(
        self,
        key: str,
        value: Any,
        timestamp: float,
        window_seconds: float = 2.0,  # 默认2秒窗口
    ):
        """追加时序历史，自动清理过期数据"""
        # 追加数据
        self._temporal_history[key].append((timestamp, value))

        # 自动清理超过窗口的数据
        cutoff_time = timestamp - window_seconds
        while self._temporal_history[key][0][0] < cutoff_time:
            self._temporal_history[key].popleft()
```

---

### 3. ⭐ 流隔离设计

#### 问题

多路视频流的时序分析必须独立隔离，观察"2秒内的推理结果"是针对单个流的。

#### 解决方案

**架构设计**：

```text
全局队列（所有客户端的推理结果）
    ↓
TemporalWorker Pool (无状态，负载均衡)
    ↓
通过 client_id 路由到对应的 ClientState
    ↓
每个 ClientState 维护独立的时序历史（2秒窗口）
```

**关键保证**：

- ✅ **流隔离**：每个客户端的 `ClientState` 是独立的，天然隔离
- ✅ **Worker无状态**：Worker不维护状态，只负责计算和路由
- ✅ **负载均衡**：多个Worker从全局队列竞争获取任务
- ✅ **时间窗口准确**：`ClientState` 按时间戳维护历史，精确计算2秒窗口

**对比示例**：

```python
# ❌ 错误设计：流混合（不推荐）
global_history = [
    (Client1, t1, 气泡✓),
    (Client2, t1, 气泡✗),
    (Client1, t2, 气泡✓),
]
# ⚠️ 不同客户端的数据混在一起，无法准确判断单个流的状态

# ✅ 正确设计：流隔离（推荐）
client1.state._temporal_history["bubble"] = [(t1, ✓), (t2, ✓), (t3, ✓)]
client2.state._temporal_history["bubble"] = [(t1, ✗), (t2, ✗), (t3, ✗)]
# ✅ 每个客户端有独立的时序历史，互不干扰
```

---

### 4. ⭐ 写回数据结构完善

#### 问题

当前写回数据缺少前端所需的 message 字段，前端需要额外处理。

#### 解决方案

新增完整的 `WriteBackData` 数据结构：

```python
@dataclass
class WriteBackData:
    """完整的写回数据包"""
    client_id: str
    timestamp: float
    stage: str

    # 1. 处理后的帧（可视化后）
    processed_frame: np.ndarray

    # 2. 推理结果JSON（完整结构化数据）
    inference_result: Dict[str, Any]

    # 3. 返回给前端的消息（精简版）
    frontend_message: FrontendMessage

    # 4. 时序分析结果（可选，用于调试）
    temporal_result: Optional[TemporalAnalysisResult] = None

@dataclass
class FrontendMessage:
    """返回给前端的消息（精简版）"""
    client_id: str
    timestamp: float
    stage: str

    # 检测结果（布尔值）
    detections: Dict[str, bool]  # {"bubble": True, "bending": False}

    # 置信度
    confidences: Dict[str, float]  # {"bubble": 0.95, "bending": 0.23}

    # 状态提示
    status_message: str  # "检测到气泡 (连续3帧)"

    # 阶段进度
    progress: Dict[str, Any]  # {
                                #   "current_step": "气泡检测",
                                #   "completed": False,
                                #   "events": ["连续3帧检测到气泡"]
                                # }
```

**收益**：

- ✅ 前端无需额外处理，直接使用 `frontend_message`
- ✅ 包含完整推理结果JSON，支持高级分析
- ✅ 包含可视化后的帧，支持实时展示
- ✅ 包含时序分析结果，支持调试和监控

---

## 性能预估

### 原架构（当前）

| 阶段 | 耗时 | 说明 |
|-----|------|------|
| 批量推理 | 20-50ms | GPU密集，已优化 |
| 状态更新 | 1-2ms | CPU轻量 |
| **可视化** | **15-30ms** | **瓶颈**，CPU密集 |
| 队列写入 | 1-2ms | I/O轻量 |
| **总耗时** | **37-84ms** | **吞吐：11-27 fps** |

### 新架构（改进后）

| 阶段 | 耗时 | 并行性 | 说明 |
|-----|------|--------|------|
| 批量推理 | 20-50ms | ✅ CUDA Stream | 无变化 |
| 投递队列 | <1ms | ✅ 异步 | 解放推理线程 |
| **InferWorker总耗时** | **21-51ms** | - | **吞吐：19-47 fps** |
| 时序分析 | 2-5ms | ✅ 多线程池 | 异步执行 |
| 可视化 | 15-30ms | ✅ 多线程池 | 异步执行 |
| 队列写入 | 1-2ms | ✅ 多线程池 | 异步执行 |

**性能提升**：

- ✅ 推理吞吐提升：**40-75%**（从11-27fps提升到19-47fps）
- ✅ 端到端延迟：略有增加（5-10ms），但可接受
- ✅ 支持更多路视频流：从20路提升到 **30-40路**

---

## 内存占用分析

### 时间窗口内存占用

假设：
- 推理帧率：10 fps
- 时间窗口：2秒
- 每个检测结果：约50字节（布尔值+元数据）

```python
# 每个客户端的内存占用（2秒窗口）
frames_per_window = 10 fps * 2 seconds = 20 frames
memory_per_client = 20 * 50 bytes = 1 KB

# 支持40路视频流
total_memory = 40 clients * 1 KB = 40 KB  # 可忽略不计
```

**结论**：时间窗口内存占用极小，不会成为瓶颈。

---

## 实施计划

### 阶段1：数据结构扩展（1-2小时）

- [ ] 扩展 `InferenceResult`、`TemporalAnalysisResult`、`FrontendMessage`
- [ ] 扩展 `ClientState`：添加 `push_temporal_history()` 等方法

### 阶段2：时序分析器实现（2-3小时）

- [ ] 创建 `TemporalAnalyzer` 抽象基类
- [ ] 实现 `DefaultTemporalAnalyzer`（支持3种模式）
- [ ] 单元测试

### 阶段3：Worker线程池实现（3-4小时）

- [ ] 实现 `TemporalWorker` 和 `TemporalWorkerPool`
- [ ] 实现 `VisualizationWorker` 和 `VisualizationWorkerPool`
- [ ] 实现 `WriteBackWorker` 和 `WriteBackWorkerPool`

### 阶段4：集成到 ModelWorkerService（2-3小时）

- [ ] 精简 `_inference_loop()` 逻辑
- [ ] 初始化各 Worker 线程池
- [ ] 连接队列管道

### 阶段5：测试和性能调优（2-3小时）

- [ ] 单元测试
- [ ] 集成测试（20-40路视频流）
- [ ] 性能测试（对比新旧架构）

### 阶段6：文档更新（1小时）

- [ ] 更新架构文档
- [ ] 添加迁移指南

**总时长**：约 12-16 小时

---

## 向后兼容策略

使用特性开关（feature flag），逐步迁移：

```python
class ModelWorkerService:
    def __init__(
        self,
        ...,
        use_async_pipeline: bool = False,  # 新增：异步管道开关
    ):
        if use_async_pipeline:
            # 使用新架构（异步管道）
            self._init_async_pipeline()
        else:
            # 使用原架构（同步）
            self._init_sync_pipeline()
```

**迁移步骤**：

1. 默认关闭新架构（`use_async_pipeline=False`）
2. 在测试环境开启新架构
3. 验证通过后，逐步在生产环境启用
4. 最终弃用旧架构代码

---

## 常见问题

### Q1: 异步管道会增加延迟吗？

**A**: 会略有增加（5-10ms），但吞吐提升40-75%，整体性能更优。适合高并发场景。

### Q2: 如何保证时序分析的正确性？

**A**:

- 使用队列（Queue）保证FIFO顺序
- ClientState内置锁保证线程安全
- 时序分析器独立测试验证
- 流隔离设计确保客户端数据不会混淆

### Q3: 2秒时间窗口会占用大量内存吗？

**A**: 不会。每个客户端约1KB（20帧 × 50字节），40路视频流仅占40KB，可忽略不计。

### Q4: 如果推理帧率不是10fps怎么办？

**A**: `ClientState` 支持动态设置窗口大小，会自动根据时间戳清理过期数据，与帧率无关。

### Q5: 如何监控各阶段性能？

**A**:

- 为每个队列添加深度监控
- 为每个Worker添加性能统计（耗时、吞吐）
- 使用 Prometheus/Grafana 可视化

---

## 关键优势总结

| 优势 | 说明 |
|-----|------|
| ✅ **解耦关注点** | 推理、时序、可视化、写回各司其职 |
| ✅ **并行化处理** | 可视化和写回不再阻塞推理线程 |
| ✅ **流隔离保证** | 每个客户端的时序状态完全独立 |
| ✅ **2秒窗口支持** | 支持复杂时序分析（滑动窗口、趋势检测等） |
| ✅ **易于扩展** | 时序分析独立模块，支持未来复杂算法（LSTM等） |
| ✅ **数据完整性** | 写回结构包含前端所需的所有信息 |
| ✅ **性能大幅提升** | 吞吐提升40-75%，支持30-40路视频流 |
| ✅ **内存占用低** | 2秒窗口仅占40KB（40路流） |
| ✅ **向后兼容** | 特性开关逐步迁移，风险可控 |

---

## 参考资料

- [完整改进方案](./INFERENCE_SERVICE_IMPROVEMENT_PLAN.md) - 详细设计文档
- [当前架构文档](./INFERENCE_SERVICE_OVERVIEW.md) - 现有架构说明
- [详细架构文档](./INFERENCE_SERVICE_ARCHITECTURE.md) - 架构细节

---

**文档版本**: v1.0
**创建日期**: 2026-01-21
**作者**: Claude Code Assistant
