# 推理服务架构改进方案

## 改进目标

  1. **性能瓶颈**：可视化处理占用40-50%耗时，阻塞推理线程
  2. **职责耦合**：InferWorker承担推理、时序、可视化、写回等多重职责
  3. **扩展困难**：时序分析逻辑与推理逻辑耦合，未来复杂算法难以集成
  4. **数据结构不完整**：写回数据缺少前端所需的message字段

---

## 改进架构概览

### 新架构数据流

```
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
│                 │      │                 │
│ ┌─────────────┐ │      │ ┌─────────────┐ │
│ │ 1. 取batch  │ │      │ │ 1. 取batch  │ │
│ │ 2. 批量推理 │ │      │ │ 2. 批量推理 │ │
│ │ 3. 输出结果 │ │      │ │ 3. 输出结果 │ │
│ └─────────────┘ │      │ └─────────────┘ │
└────────┬────────┘      └────────┬────────┘
         │                        │
         └────────────┬───────────┘
                      ▼
         ┌────────────────────────────────┐
         │  TemporalAnalysisQueue         │  【新增】时序分析队列
         │  - 缓冲推理结果                │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  TemporalWorker Pool (2-4线程)  │  【新增】时序分析池
         │  - 消费推理结果                │
         │  - 执行时序逻辑                │
         │  - 更新 ClientState            │
         │  - 生成事件和状态变更          │
         │  - 生成前端消息                │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  VisualizationQueue            │  【新增】可视化队列
         │  - 缓冲待可视化数据            │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  VisualizationWorker Pool      │  【新增】可视化线程池
         │  (4-8线程)                     │
         │  - 异步绘制检测框              │
         │  - 异步绘制标注                │
         │  - 多线程并行处理              │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  WriteBackQueue                │  【新增】写回队列
         │  - 缓冲完整数据包              │
         └────────────┬───────────────────┘
                      ▼
         ┌────────────────────────────────┐
         │  WriteBackWorker Pool (2-4线程) │  【新增】写回线程池
         │  - 写入 ca_processed 队列       │
         │  - 写入 rt_processed 队列       │
         │  - 写入数据库（可选）          │
         └────────────────────────────────┘
```

### 关键改进点

#### 1. InferWorker 职责精简

**原职责**（过重）：
- ✅ 批量推理（GPU密集）
- ❌ 状态更新（CPU）
- ❌ 可视化处理（CPU密集，15-30ms）
- ❌ 队列写入（I/O）

**新职责**（精简）：
- ✅ 批量推理（GPU密集，20-50ms）
- ✅ 将结果投递到时序分析队列（<1ms）

**⭐ 重要改进：Stage无关设计**

不再为每个 stage 创建独立的 InferWorker，而是：
- **所有 stage 共享同一组 InferWorker**
- 每个 InferWorker 根据 `stage` 字段动态选择对应的 WorkerPool
- 通过 `stage_configs` 配置驱动，而非硬编码

```python
# 改进前：每个 stage 一个独立线程（扩展性差）
InferWorker-LEAK    → MultiModelWorkerPool(LEAK)
InferWorker-CLEAN   → MultiModelWorkerPool(CLEAN)
InferWorker-INSPECT → MultiModelWorkerPool(INSPECT)  # 新增 stage 需要新建线程

# 改进后：统一的 InferWorker Pool（扩展性好）
InferWorker Pool (2-4个通用线程)
    ↓ 根据 stage 字段路由
    ├─ MultiModelWorkerPool(LEAK)
    ├─ MultiModelWorkerPool(CLEAN)
    └─ MultiModelWorkerPool(INSPECT)  # 新增 stage 只需配置
```

**收益**：
- 推理线程解放，吞吐提升 **40-60%**
- 推理延迟降低 **30-50%**
- **新增 stage 无需修改代码，只需配置**

#### 2. TemporalWorker 时序分析专用池

**职责**：
- 消费推理结果（从 TemporalAnalysisQueue）
- 执行复杂时序逻辑（连续帧检测、滑动窗口、累计计数等）
- 更新 ClientState（状态、计数器）
- 生成事件（如"连续3帧检测到气泡"）
- 生成前端消息（FrontendMessage）

**设计要点**：
- **线程数**：2-4个线程（CPU密集型）
- **独立扩展**：未来可以集成更复杂的时序算法（LSTM、时间序列预测等）
- **解耦推理**：推理和时序完全分离，互不阻塞
- **⭐流隔离设计**：每个客户端的时序状态完全独立，支持2秒时间窗口分析

**流隔离架构**：

```text
TemporalAnalysisQueue (全局队列)
        ↓
TemporalWorker Pool (2-4线程)
        ↓
    每个Worker处理任意客户端的结果
        ↓
    通过 client_id 路由到对应的 ClientState
        ↓
    ClientState 维护该流的时序历史（2秒窗口）
```

**关键设计**：

- **全局队列**：所有客户端的推理结果进入同一个队列
- **Worker无状态**：Worker本身不存储状态，只负责计算
- **ClientState有状态**：每个客户端的时序状态存储在其 `ClientState` 中
- **时间窗口隔离**：`ClientState` 维护该客户端最近2秒的推理历史

**为什么这样设计？**

1. **流隔离保证**：每个客户端的 `ClientState` 是独立的，天然隔离
2. **Worker负载均衡**：多个Worker从全局队列竞争获取任务，自动负载均衡
3. **无状态Worker**：Worker不维护状态，方便扩缩容
4. **时间窗口准确**：`ClientState` 按时间戳维护历史，精确计算2秒窗口

**示例代码**：
```python
# app/services/inference/temporal_worker.py

from queue import Queue
from typing import Dict, List
import threading

class TemporalWorker:
    """时序分析工作线程"""

    def __init__(
        self,
        input_queue: Queue,  # 输入：推理结果
        output_queue: Queue,  # 输出：时序分析后的数据
        analyzer: TemporalAnalyzer,
        stop_event: threading.Event,
    ):
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.analyzer = analyzer
        self.stop_event = stop_event

    def run(self):
        """工作循环"""
        while not self.stop_event.is_set():
            try:
                # 1. 从队列获取推理结果
                result: InferenceResult = self.input_queue.get(timeout=0.1)

                # 2. 获取客户端状态
                cq = client_manager.get_client(result.client_id)
                if cq is None:
                    continue

                # 3. 执行时序分析
                temporal_result = self.analyzer.analyze(cq.state, result)

                # 4. 生成前端消息
                frontend_msg = self._create_frontend_message(
                    result, temporal_result
                )

                # 5. 组装数据包
                data_package = TemporalAnalysisPackage(
                    client_id=result.client_id,
                    timestamp=result.timestamp,
                    stage=result.stage,
                    inference_result=result.result,
                    temporal_result=temporal_result,
                    frontend_message=frontend_msg,
                    raw_frame=result.frame,  # 原始帧，供可视化使用
                )

                # 6. 投递到可视化队列
                self.output_queue.put(data_package)

            except Exception as e:
                print(f"[TemporalWorker] 异常: {e}")

    def _create_frontend_message(
        self,
        result: InferenceResult,
        temporal: TemporalAnalysisResult,
    ) -> FrontendMessage:
        """生成前端消息"""
        # 提取检测结果
        detections = {}
        confidences = {}

        for subtask_name, subtask_res in result.result.items():
            if isinstance(subtask_res, dict):
                detected_key = f"{subtask_name}_detected"
                detections[subtask_name] = subtask_res.get(detected_key, False)
                confidences[subtask_name] = subtask_res.get("confidence", 0.0)

        # 生成状态消息
        status_msg = self._generate_status_message(temporal)

        return FrontendMessage(
            client_id=result.client_id,
            timestamp=result.timestamp,
            stage=result.stage,
            detections=detections,
            confidences=confidences,
            status_message=status_msg,
            progress={
                "current_step": result.stage,
                "completed": temporal.step_completed,
                "events": temporal.events,
            },
        )
```

#### 3. VisualizationWorker 可视化专用池

**职责**：
- 消费时序分析后的数据包（从 VisualizationQueue）
- 取当前客户端的最新帧（原始帧流）
- 绘制最新的检测框、标注、文字信息到最新帧上
- 若没有新的检测结果，继续沿用上一次的检测结果
- 多线程并行处理多个客户端

**设计要点**：
- **线程数**：4-8个线程（CPU密集型，可并行）
- **异步化**：不阻塞推理线程
- **降帧补偿**：由于推理降帧，可视化需要从原始帧流中取最新帧
- **结果缓存**：缓存最新的检测结果，应用到后续未推理的帧上
- **可扩展**：支持不同可视化风格（debug模式、生产模式等）

**降帧可视化原理**：
```text
原始帧流（30fps）：Frame1, Frame2, Frame3, Frame4, Frame5, Frame6, Frame7, Frame8...
推理帧流（10fps）：Frame1(推理), -, -, Frame4(推理), -, -, Frame7(推理), -...

可视化策略：
1. Frame1推理后 → 缓存检测结果A → 可视化Frame1
2. Frame2/3未推理 → 取最新原始帧，使用结果A → 可视化Frame2/3
3. Frame4推理后 → 更新检测结果B → 可视化Frame4
4. Frame5/6未推理 → 取最新原始帧，使用结果B → 可视化Frame5/6
```

**示例代码**：
```python
# app/services/inference/visualization_worker.py

class VisualizationWorker:
    """可视化工作线程"""

    def __init__(
        self,
        input_queue: Queue,  # 输入：时序分析后的数据包
        output_queue: Queue,  # 输出：完整数据包（含可视化帧）
        visualizer: Visualizer,
        stop_event: threading.Event,
    ):
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.visualizer = visualizer
        self.stop_event = stop_event

        # 缓存每个客户端的最新检测结果（用于降帧补偿）
        self.latest_results = {}  # {client_id: (inference_result, temporal_result)}

    def run(self):
        """工作循环"""
        while not self.stop_event.is_set():
            try:
                # 1. 从队列获取数据包（包含推理结果）
                package: TemporalAnalysisPackage = self.input_queue.get(timeout=0.1)

                # 2. 更新该客户端的最新检测结果
                self.latest_results[package.client_id] = (
                    package.inference_result,
                    package.temporal_result,
                )

                # 3. 获取客户端的最新原始帧（而非推理时的旧帧）
                cq = client_manager.get_client(package.client_id)
                if cq is None:
                    continue

                latest_frame = cq.get_latest_frame()  # 取当前最新帧
                if latest_frame is None:
                    # 如果没有更新的帧，使用推理时的帧
                    latest_frame = package.raw_frame

                # 4. 使用最新的检测结果进行可视化
                annotated_frame = self.visualizer.visualize(
                    frame=latest_frame,  # 使用最新帧
                    inference_result=package.inference_result,
                    stage=package.stage,
                    temporal_result=package.temporal_result,
                )

                # 5. 组装完整数据包
                write_back_data = WriteBackData(
                    client_id=package.client_id,
                    timestamp=package.timestamp,  # 保持推理时间戳
                    stage=package.stage,
                    processed_frame=annotated_frame,
                    inference_result=package.inference_result,
                    frontend_message=package.frontend_message,
                    temporal_result=package.temporal_result,
                )

                # 6. 投递到写回队列
                self.output_queue.put(write_back_data)

            except Exception as e:
                print(f"[VisualizationWorker] 异常: {e}")

    def visualize_with_cached_result(
        self,
        client_id: str,
        current_frame: np.ndarray
    ) -> Optional[np.ndarray]:
        """使用缓存的检测结果可视化当前帧（用于未推理的中间帧）

        Args:
            client_id: 客户端ID
            current_frame: 当前最新帧

        Returns:
            可视化后的帧，若无缓存结果则返回None
        """
        if client_id not in self.latest_results:
            return None

        inference_result, temporal_result = self.latest_results[client_id]

        # 使用缓存的检测结果绘制当前帧
        return self.visualizer.visualize(
            frame=current_frame,
            inference_result=inference_result,
            stage=temporal_result.stage if temporal_result else "UNKNOWN",
            temporal_result=temporal_result,
        )
```

#### 4. WriteBackWorker 写回专用池

**职责**：
- 消费完整数据包（从 WriteBackQueue）
- 写入 ClientQueues（ca_processed、rt_processed）
- 写入数据库（可选，记录推理历史）

**设计要点**：
- **线程数**：2-4个线程（I/O密集型）
- **容错**：安全检查客户端是否存在
- **批量写入**：可以批量写入数据库提升性能

**示例代码**：
```python
# app/services/inference/writeback_worker.py

class WriteBackWorker:
    """写回工作线程"""

    def __init__(
        self,
        input_queue: Queue,  # 输入：完整数据包
        stop_event: threading.Event,
        enable_db_write: bool = False,
    ):
        self.input_queue = input_queue
        self.stop_event = stop_event
        self.enable_db_write = enable_db_write

    def run(self):
        """工作循环"""
        while not self.stop_event.is_set():
            try:
                # 1. 从队列获取完整数据包
                data: WriteBackData = self.input_queue.get(timeout=0.1)

                # 2. 安全检查：客户端可能已清理
                cq = client_manager.get_client(data.client_id)
                if cq is None:
                    continue

                # 3. 构造 FrameData
                frame_data = FrameData(
                    timestamp=data.timestamp,
                    frame=data.processed_frame,
                    inference_result=data.inference_result,
                    keypoints=None,  # 如果需要，从 inference_result 提取
                )

                # 4. 写入队列
                cq.append_ca_processed(frame_data)
                cq.append_rt_processed(frame_data)

                # 5. 写入数据库（可选）
                if self.enable_db_write:
                    self._write_to_database(data)

            except Exception as e:
                print(f"[WriteBackWorker] 异常: {e}")

    def _write_to_database(self, data: WriteBackData):
        """写入数据库（记录推理历史）"""
        # TODO: 实现数据库写入逻辑
        # 可以记录：
        # - 推理结果（JSON）
        # - 时序事件
        # - 前端消息
        # - 可视化帧的存储路径（可选）
        pass
```

---

## 流隔离与时间窗口设计

### 核心问题

**问题**：时序分析需要观察"2秒内的推理结果"，如何针对每个输入流（客户端）独立隔离？

**答案**：通过 `ClientState` 维护每个客户端的时间窗口历史。

### 设计方案

#### 1. ClientState 扩展：支持时间窗口历史

```python
# app/services/client.py 扩展

from collections import deque
from typing import Any, Deque, List, Tuple
import time

class ClientState:
    """客户端业务状态管理类（扩展版）"""

    def __init__(self, client_id: str, initial_stage: str = "LEAK"):
        # ... 原有字段 ...

        # 时序历史队列（新增）
        # 存储格式：(timestamp, data)
        self._temporal_history: Dict[str, Deque[Tuple[float, Any]]] = {}
        self._history_window_seconds: float = 2.0  # 默认2秒窗口

    def push_temporal_history(
        self,
        key: str,
        value: Any,
        timestamp: float,
        window_seconds: float = None,
    ) -> None:
        """追加时序历史（自动清理过期数据）

        Args:
            key: 历史队列的键（如 "bubble_detections"）
            value: 要存储的值（如 True/False 或检测结果字典）
            timestamp: 当前时间戳
            window_seconds: 时间窗口大小（秒），默认使用 self._history_window_seconds
        """
        with self._lock:
            if key not in self._temporal_history:
                self._temporal_history[key] = deque()

            # 追加新数据
            self._temporal_history[key].append((timestamp, value))

            # 清理过期数据（超过窗口的数据）
            window = window_seconds or self._history_window_seconds
            cutoff_time = timestamp - window

            # 从队列头部移除过期数据
            while (
                self._temporal_history[key]
                and self._temporal_history[key][0][0] < cutoff_time
            ):
                self._temporal_history[key].popleft()

    def get_temporal_history(
        self,
        key: str,
        timestamp: float = None,
        window_seconds: float = None,
    ) -> List[Tuple[float, Any]]:
        """获取时序历史（返回窗口内的数据）

        Args:
            key: 历史队列的键
            timestamp: 当前时间戳（用于过滤），如果为None则使用当前时间
            window_seconds: 时间窗口大小（秒）

        Returns:
            [(timestamp, value), ...] 列表（窗口内的数据）
        """
        with self._lock:
            if key not in self._temporal_history:
                return []

            # 如果未指定时间戳，使用当前时间
            if timestamp is None:
                timestamp = time.time()

            # 计算截止时间
            window = window_seconds or self._history_window_seconds
            cutoff_time = timestamp - window

            # 过滤窗口内的数据
            return [
                (ts, val)
                for ts, val in self._temporal_history[key]
                if ts >= cutoff_time
            ]

    def get_temporal_values(
        self,
        key: str,
        timestamp: float = None,
        window_seconds: float = None,
    ) -> List[Any]:
        """获取时序历史的值列表（不包含时间戳）"""
        history = self.get_temporal_history(key, timestamp, window_seconds)
        return [val for _, val in history]

    def clear_temporal_history(self, key: str) -> None:
        """清空时序历史"""
        with self._lock:
            if key in self._temporal_history:
                self._temporal_history[key].clear()

    def set_history_window(self, window_seconds: float) -> None:
        """设置历史窗口大小"""
        with self._lock:
            self._history_window_seconds = window_seconds
```

#### 2. 时序分析器使用时间窗口

```python
# app/services/inference/temporal_analyzer.py

class DefaultTemporalAnalyzer(TemporalAnalyzer):

    def _analyze_sliding_window(
        self,
        state: ClientState,
        subtask_name: str,
        subtask_res: Dict[str, Any],
        config: Dict[str, Any],
        current_timestamp: float,
    ) -> Optional[str]:
        """滑动窗口模式（支持2秒时间窗口）"""
        detected_key = f"{subtask_name}_detected"
        detected = subtask_res.get(detected_key, False)

        # 配置参数
        window_seconds = config.get("window_seconds", 2.0)  # 默认2秒窗口
        ratio_threshold = config.get("ratio", 0.7)  # 默认70%比例

        history_key = f"{subtask_name}_window"

        # 1. 追加当前检测结果到历史
        state.push_temporal_history(
            key=history_key,
            value=detected,
            timestamp=current_timestamp,
            window_seconds=window_seconds,
        )

        # 2. 获取窗口内的所有检测结果
        values = state.get_temporal_values(
            key=history_key,
            timestamp=current_timestamp,
            window_seconds=window_seconds,
        )

        # 3. 计算窗口内的检测比例
        if len(values) == 0:
            return None

        detected_count = sum(1 for v in values if v)
        detection_ratio = detected_count / len(values)

        # 4. 判断是否满足阈值
        if detection_ratio >= ratio_threshold:
            state.mark_step_completed()
            return (
                f"滑动窗口检测：最近{window_seconds}秒内{len(values)}帧，"
                f"检测到{detected_count}次{subtask_name}（{detection_ratio:.1%}）"
            )

        return None
```

#### 3. 使用示例

```python
# 配置时序分析器
temporal_config = {
    "LEAK": {
        "bubble": {
            "mode": "sliding_window",
            "window_seconds": 2.0,    # 观察最近2秒
            "ratio": 0.7,              # 70%的帧检测到气泡
        },
        "bending": {
            "mode": "sliding_window",
            "window_seconds": 2.0,
            "ratio": 0.5,              # 50%的帧检测到折弯
        },
    },
    "CLEAN": {
        "quality": {
            "mode": "sliding_window",
            "window_seconds": 2.0,
            "ratio": 0.8,              # 80%的帧清洁
        },
    },
}

analyzer = DefaultTemporalAnalyzer(config=temporal_config)
```

### 流隔离保证

#### 架构对比：流隔离 vs 流混合

##### 错误设计：流混合（不推荐）

```text
全局时序历史队列（所有客户端共用）
   ↓
[Client1:气泡✓, Client2:气泡✗, Client1:气泡✓, Client3:气泡✓]
   ↓
⚠️ 问题：不同客户端的数据混在一起，无法准确判断单个流的状态
```

##### 正确设计：流隔离（推荐）

```text
Client1.state._temporal_history["bubble"] = [(t1, ✓), (t2, ✓), (t3, ✓)]
Client2.state._temporal_history["bubble"] = [(t1, ✗), (t2, ✗), (t3, ✗)]
Client3.state._temporal_history["bubble"] = [(t1, ✓), (t2, ✓), (t3, ✗)]
   ↓
✅ 每个客户端有独立的时序历史，互不干扰
✅ 准确计算每个流的2秒窗口统计
```

#### 数据隔离

```python
# 每个客户端有独立的 ClientState
client_1_state = ClientState(client_id="client_1")
client_2_state = ClientState(client_id="client_2")

# 推理结果 1：客户端1检测到气泡
result_1 = InferenceResult(
    client_id="client_1",
    timestamp=100.0,
    stage="LEAK",
    result={"bubble": {"bubble_detected": True}},
)

# 推理结果 2：客户端2未检测到气泡
result_2 = InferenceResult(
    client_id="client_2",
    timestamp=100.0,
    stage="LEAK",
    result={"bubble": {"bubble_detected": False}},
)

# TemporalWorker 处理时，会根据 client_id 路由到对应的 ClientState
# 两个客户端的时序历史完全独立，互不影响
analyzer.analyze(client_1_state, result_1)  # 影响 client_1 的状态
analyzer.analyze(client_2_state, result_2)  # 影响 client_2 的状态
```

#### Worker负载均衡

```python
# TemporalWorker 工作循环（支持流隔离）
def run(self):
    while not self.stop_event.is_set():
        try:
            # 从全局队列获取推理结果（可能来自任意客户端）
            result: InferenceResult = self.input_queue.get(timeout=0.1)

            # 根据 client_id 获取对应的 ClientState
            cq = client_manager.get_client(result.client_id)
            if cq is None:
                continue

            # 执行时序分析（状态隔离在 ClientState 中）
            temporal_result = self.analyzer.analyze(
                state=cq.state,        # 每个客户端独立的状态
                result=result,
                current_timestamp=result.timestamp,
            )

            # ... 后续处理 ...

        except Exception as e:
            print(f"[TemporalWorker] 异常: {e}")
```

### 性能考虑

#### 时间窗口大小与内存占用

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

#### 自动清理过期数据

`push_temporal_history()` 方法在每次追加数据时，会自动清理超过窗口的历史数据，确保：

- 内存占用稳定
- 查询效率高（只包含窗口内数据）
- 无需手动管理

### 扩展：支持更复杂的时序模式

#### 模式1：时间加权检测

```python
def _analyze_time_weighted(
    self,
    state: ClientState,
    subtask_name: str,
    subtask_res: Dict[str, Any],
    config: Dict[str, Any],
    current_timestamp: float,
) -> Optional[str]:
    """时间加权检测：近期检测结果权重更高"""
    detected = subtask_res.get(f"{subtask_name}_detected", False)
    confidence = subtask_res.get("confidence", 0.0)

    history_key = f"{subtask_name}_weighted"
    window_seconds = config.get("window_seconds", 2.0)

    # 追加检测结果（带置信度）
    state.push_temporal_history(
        key=history_key,
        value={"detected": detected, "confidence": confidence},
        timestamp=current_timestamp,
        window_seconds=window_seconds,
    )

    # 获取窗口内历史
    history = state.get_temporal_history(history_key, current_timestamp, window_seconds)

    if len(history) == 0:
        return None

    # 计算时间加权得分
    total_weight = 0.0
    weighted_score = 0.0

    for ts, data in history:
        age = current_timestamp - ts  # 数据年龄（秒）
        weight = 1.0 / (1.0 + age)     # 越新的数据权重越高

        if data["detected"]:
            weighted_score += weight * data["confidence"]

        total_weight += weight

    avg_score = weighted_score / total_weight
    threshold = config.get("threshold", 0.6)

    if avg_score >= threshold:
        state.mark_step_completed()
        return f"时间加权检测：得分{avg_score:.2f}（阈值{threshold}）"

    return None
```

#### 模式2：趋势检测

```python
def _analyze_trend(
    self,
    state: ClientState,
    subtask_name: str,
    subtask_res: Dict[str, Any],
    config: Dict[str, Any],
    current_timestamp: float,
) -> Optional[str]:
    """趋势检测：检测数量逐渐增加/减少"""
    detected_count = subtask_res.get("detection_count", 0)

    history_key = f"{subtask_name}_trend"
    window_seconds = config.get("window_seconds", 2.0)

    # 追加检测数量
    state.push_temporal_history(
        key=history_key,
        value=detected_count,
        timestamp=current_timestamp,
        window_seconds=window_seconds,
    )

    # 获取窗口内历史
    values = state.get_temporal_values(history_key, current_timestamp, window_seconds)

    if len(values) < 5:  # 至少需要5个数据点
        return None

    # 简单趋势判断：比较前半部分和后半部分的平均值
    mid = len(values) // 2
    early_avg = sum(values[:mid]) / mid
    recent_avg = sum(values[mid:]) / (len(values) - mid)

    trend_type = config.get("trend_type", "increasing")  # "increasing" or "decreasing"
    threshold_change = config.get("threshold_change", 1.5)  # 变化倍数

    if trend_type == "increasing" and recent_avg >= early_avg * threshold_change:
        return f"检测到上升趋势：{early_avg:.1f} → {recent_avg:.1f}"
    elif trend_type == "decreasing" and recent_avg <= early_avg / threshold_change:
        return f"检测到下降趋势：{early_avg:.1f} → {recent_avg:.1f}"

    return None
```

---

## 数据结构设计

### 1. InferenceResult（保持不变）

```python
@dataclass
class InferenceResult:
    """推理结果：关联客户端"""
    client_id: str
    timestamp: float
    stage: str
    result: Dict[str, Any]  # subtask_results
    annotated_frame: Optional[np.ndarray] = None  # 可选
```

### 2. TemporalAnalysisResult（新增）

```python
@dataclass
class TemporalAnalysisResult:
    """时序分析结果"""
    client_id: str
    timestamp: float

    # 状态变更
    stage_changed: bool  # 是否切换stage
    new_stage: Optional[str]  # 新的stage（如果切换）
    step_completed: bool  # 当前步骤是否完成

    # 触发的事件
    events: List[str]  # 例如：["连续3帧检测到气泡", "步骤完成"]

    # 状态更新（用于调试和监控）
    state_snapshot: Dict[str, Any]  # ClientState 快照
```

### 3. FrontendMessage（新增）

```python
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

### 4. TemporalAnalysisPackage（新增）

```python
@dataclass
class TemporalAnalysisPackage:
    """时序分析后的数据包（传递给可视化线程）"""
    client_id: str
    timestamp: float
    stage: str

    # 推理结果
    inference_result: Dict[str, Any]

    # 时序分析结果
    temporal_result: TemporalAnalysisResult

    # 前端消息
    frontend_message: FrontendMessage

    # 原始帧（供可视化使用）
    raw_frame: np.ndarray
```

### 5. WriteBackData（新增）

```python
@dataclass
class WriteBackData:
    """完整的写回数据包（最终形态）"""
    client_id: str
    timestamp: float
    stage: str

    # 1. 处理后的帧（可视化后）
    processed_frame: np.ndarray

    # 2. 推理结果JSON（完整结构化数据）
    inference_result: Dict[str, Any]  # 包含所有子任务结果

    # 3. 返回给前端的消息
    frontend_message: FrontendMessage

    # 4. 时序分析结果（可选，用于调试）
    temporal_result: Optional[TemporalAnalysisResult] = None
```

---

## 时序分析器设计

### TemporalAnalyzer 接口

```python
# app/services/inference/temporal_analyzer.py

from abc import ABC, abstractmethod
from typing import Dict, List

class TemporalAnalyzer(ABC):
    """时序分析器抽象基类"""

    @abstractmethod
    def analyze(
        self,
        state: ClientState,
        result: InferenceResult,
    ) -> TemporalAnalysisResult:
        """
        分析推理结果，更新时序状态

        Args:
            state: 客户端状态
            result: 推理结果

        Returns:
            TemporalAnalysisResult: 时序分析结果
        """
        pass


class DefaultTemporalAnalyzer(TemporalAnalyzer):
    """默认时序分析器实现"""

    def __init__(self, config: Dict[str, Any]):
        """
        Args:
            config: 配置字典，例如：
            {
                "LEAK": {
                    "bubble": {
                        "mode": "consecutive",  # 连续帧模式
                        "threshold": 3,          # 连续3帧
                    },
                    "bending": {
                        "mode": "accumulated",   # 累计计数模式
                        "threshold": 5,          # 累计5次
                    },
                },
                "CLEAN": {
                    "quality": {
                        "mode": "sliding_window",  # 滑动窗口模式
                        "window_size": 10,          # 窗口大小
                        "ratio": 0.8,               # 80%比例
                    },
                },
            }
        """
        self.config = config

    def analyze(
        self,
        state: ClientState,
        result: InferenceResult,
    ) -> TemporalAnalysisResult:
        """执行时序分析"""
        stage = result.stage
        stage_config = self.config.get(stage, {})

        events = []
        stage_changed = False
        new_stage = None
        step_completed = False

        # 遍历所有子任务结果
        for subtask_name, subtask_res in result.result.items():
            if not isinstance(subtask_res, dict):
                continue

            # 获取该子任务的配置
            subtask_cfg = stage_config.get(subtask_name, {})
            mode = subtask_cfg.get("mode", "consecutive")

            # 根据模式执行时序逻辑
            if mode == "consecutive":
                event = self._analyze_consecutive(
                    state, subtask_name, subtask_res, subtask_cfg
                )
                if event:
                    events.append(event)

            elif mode == "accumulated":
                event = self._analyze_accumulated(
                    state, subtask_name, subtask_res, subtask_cfg
                )
                if event:
                    events.append(event)

            elif mode == "sliding_window":
                event = self._analyze_sliding_window(
                    state, subtask_name, subtask_res, subtask_cfg
                )
                if event:
                    events.append(event)

        # 判断步骤是否完成
        step_completed = state.is_step_completed()

        # 获取状态快照
        state_snapshot = state.to_dict()

        return TemporalAnalysisResult(
            client_id=result.client_id,
            timestamp=result.timestamp,
            stage_changed=stage_changed,
            new_stage=new_stage,
            step_completed=step_completed,
            events=events,
            state_snapshot=state_snapshot,
        )

    def _analyze_consecutive(
        self,
        state: ClientState,
        subtask_name: str,
        subtask_res: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Optional[str]:
        """连续帧检测模式"""
        detected_key = f"{subtask_name}_detected"
        detected = subtask_res.get(detected_key, False)
        threshold = config.get("threshold", 3)

        counter_key = f"continuous_{subtask_name}"

        if detected:
            count = state.increment_counter(counter_key)
            if count >= threshold:
                state.mark_step_completed()
                return f"连续{count}帧检测到{subtask_name}"
        else:
            state.reset_counter(counter_key)

        return None

    def _analyze_accumulated(
        self,
        state: ClientState,
        subtask_name: str,
        subtask_res: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Optional[str]:
        """累计计数模式"""
        detected_key = f"{subtask_name}_detected"
        detected = subtask_res.get(detected_key, False)
        threshold = config.get("threshold", 5)

        counter_key = f"total_{subtask_name}"

        if detected:
            count = state.increment_counter(counter_key)
            if count >= threshold:
                return f"累计{count}次检测到{subtask_name}"

        return None

    def _analyze_sliding_window(
        self,
        state: ClientState,
        subtask_name: str,
        subtask_res: Dict[str, Any],
        config: Dict[str, Any],
    ) -> Optional[str]:
        """滑动窗口模式"""
        # TODO: 需要扩展 ClientState 支持历史队列
        # 参考文档第536-567行的实现
        pass
```

---

## 性能预估

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

**性能提升**：
- 推理吞吐提升：**40-75%**（从11-27fps提升到19-47fps）
- 端到端延迟：略有增加（5-10ms），但可接受
- 支持更多路视频流：从20路提升到 **30-40路**

---

## 实施计划

### 阶段1：数据结构扩展（1-2小时）

1. 扩展 `app/services/inference/models.py`：
   - 添加 `TemporalAnalysisResult`
   - 添加 `FrontendMessage`
   - 添加 `TemporalAnalysisPackage`
   - 添加 `WriteBackData`

2. 扩展 `FrameData`（可选）：
   - 添加 `frontend_message` 字段

### 阶段2：时序分析器实现（2-3小时）

1. 创建 `app/services/inference/temporal_analyzer.py`：
   - 实现 `TemporalAnalyzer` 抽象基类
   - 实现 `DefaultTemporalAnalyzer`
   - 支持三种模式：连续帧、累计计数、滑动窗口

2. 扩展 `ClientState`（如需滑动窗口）：
   - 添加 `push_to_history()` 方法
   - 添加 `get_history()` 方法
   - 添加 `clear_history()` 方法

### 阶段3：Worker线程池实现（3-4小时）

1. 创建 `app/services/inference/temporal_worker.py`：
   - 实现 `TemporalWorker`
   - 实现 `TemporalWorkerPool`

2. 创建 `app/services/inference/visualization_worker.py`：
   - 实现 `VisualizationWorker`
   - 实现 `VisualizationWorkerPool`

3. 创建 `app/services/inference/writeback_worker.py`：
   - 实现 `WriteBackWorker`
   - 实现 `WriteBackWorkerPool`

### 阶段4：集成到 ModelWorkerService（2-3小时）

1. 修改 `app/services/inference/service.py`：
   - 精简 `_inference_loop()` 逻辑
   - 初始化各 Worker 线程池
   - 连接队列管道

2. 更新工厂函数 `factory.py`：
   - 添加配置参数（temporal_threads、vis_threads、writeback_threads）
   - 添加时序分析器配置

### 阶段5：测试和性能调优（2-3小时）

1. 单元测试：
   - 测试时序分析器各模式
   - 测试 Worker 线程池

2. 集成测试：
   - 测试多路视频流（20-40路）
   - 测试客户端动态加入/离开

3. 性能测试：
   - 对比新旧架构吞吐量和延迟
   - 调整线程池大小

### 阶段6：文档更新（1小时）

1. 更新架构文档：
   - `INFERENCE_SERVICE_OVERVIEW.md`
   - `INFERENCE_SERVICE_ARCHITECTURE.md`

2. 添加迁移指南

---

## 向后兼容

**策略**：使用特性开关（feature flag），逐步迁移

```python
# app/services/inference/service.py

class ModelWorkerService:
    def __init__(
        self,
        ...,
        use_async_pipeline: bool = False,  # 新增：异步管道开关
    ):
        self.use_async_pipeline = use_async_pipeline

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

**A**: 会略有增加（5-10ms），但吞吐提升40-75%，整体性能更优。

### Q2: 如何保证时序分析的正确性？

**A**:
- 使用队列（Queue）保证FIFO顺序
- ClientState内置锁保证线程安全
- 时序分析器独立测试验证

### Q3: 可视化线程池大小如何设置？

**A**:
- 轻量可视化：4个线程
- 复杂可视化：8个线程
- 根据CPU核心数调整（建议不超过核心数的50%）

### Q4: 如何监控各阶段性能？

**A**:
- 为每个队列添加深度监控
- 为每个Worker添加性能统计（耗时、吞吐）
- 使用 Prometheus/Grafana 可视化

---

## 参考资料

- [当前架构文档](./INFERENCE_SERVICE_OVERVIEW.md)
- [详细架构文档](./INFERENCE_SERVICE_ARCHITECTURE.md)
- [Pipeline基类设计](./PIPELINE_BASE.md)

---

**文档版本**: v1.0
**创建日期**: 2026-01-21
**作者**: Claude Code Assistant
