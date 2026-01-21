"""
容器，管理每个客户端的队列。提供客户端数据存取接口。
"""

from collections import deque
from typing import Deque, Optional, List, Tuple, Any, Dict
import time
import numpy as np
import threading

from app.models.frame import FrameData
from app.models.task import Task as CleaningTask


class ClientState:
    """客户端业务状态管理类。

    存储单个客户端的关键业务状态，包括：
    - 当前所处的任务阶段 (stage)
    - 步骤完成状态
    - 业务相关的计数器/状态机
    - 推理结果的时序统计
    - 时间窗口历史队列（支持滑动窗口分析）

    设计目标：
    - 与 ClientQueues 分离，专注业务逻辑状态
    - 线程安全（内部使用锁保护）
    - 便于 ModelWorkerService 读取和更新
    - 支持2秒时间窗口的时序分析
    """

    def __init__(self, client_id: str, initial_stage: str = "LEAK"):
        """
        Args:
            client_id: 客户端标识
            initial_stage: 初始任务阶段（LEAK/CLEAN/etc.）
        """
        self.client_id = client_id

        # 线程锁（保护状态更新）
        self._lock = threading.RLock()

        # 核心业务状态
        self._stage: str = initial_stage  # 当前阶段
        self._step_completed: bool = False  # 当前步骤是否完成
        self._last_update_time: float = time.time()

        # 自定义状态字典（业务相关）
        self._custom_state: Dict[str, Any] = {}

        # 时序统计（例如：连续检测到气泡的帧数）
        self._sequence_counters: Dict[str, int] = {}

        # 时序历史队列（新增）- 存储格式：(timestamp, data)
        self._temporal_history: Dict[str, Deque[Tuple[float, Any]]] = {}
        self._history_window_seconds: float = 2.0  # 默认2秒窗口

    # ---- 线程安全的访问接口 ----

    def get_stage(self) -> str:
        """获取当前阶段"""
        with self._lock:
            return self._stage

    def set_stage(self, stage: str) -> None:
        """设置当前阶段"""
        with self._lock:
            if self._stage != stage:
                self._stage = stage
                self._step_completed = False  # 切换阶段时重置完成状态
                self._last_update_time = time.time()

    def is_step_completed(self) -> bool:
        """判断当前步骤是否完成"""
        with self._lock:
            return self._step_completed

    def mark_step_completed(self) -> None:
        """标记当前步骤完成"""
        with self._lock:
            self._step_completed = True
            self._last_update_time = time.time()

    def reset_step(self) -> None:
        """重置步骤状态（例如阶段切换后）"""
        with self._lock:
            self._step_completed = False
            self._sequence_counters.clear()
            self._last_update_time = time.time()

    # ---- 自定义状态管理 ----

    def set_custom(self, key: str, value: Any) -> None:
        """设置自定义状态"""
        with self._lock:
            self._custom_state[key] = value
            self._last_update_time = time.time()

    def get_custom(self, key: str, default: Any = None) -> Any:
        """获取自定义状态"""
        with self._lock:
            return self._custom_state.get(key, default)

    def update_custom(self, updates: Dict[str, Any]) -> None:
        """批量更新自定义状态"""
        with self._lock:
            self._custom_state.update(updates)
            self._last_update_time = time.time()

    # ---- 时序计数器 ----

    def increment_counter(self, key: str, delta: int = 1) -> int:
        """递增计数器，返回新值"""
        with self._lock:
            self._sequence_counters[key] = self._sequence_counters.get(key, 0) + delta
            return self._sequence_counters[key]

    def get_counter(self, key: str, default: int = 0) -> int:
        """获取计数器值"""
        with self._lock:
            return self._sequence_counters.get(key, default)

    def reset_counter(self, key: str) -> None:
        """重置计数器"""
        with self._lock:
            self._sequence_counters[key] = 0

    # ---- 时间窗口历史管理（新增）----

    def push_temporal_history(
        self,
        key: str,
        value: Any,
        timestamp: float,
        window_seconds: Optional[float] = None,
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
        timestamp: Optional[float] = None,
        window_seconds: Optional[float] = None,
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
        timestamp: Optional[float] = None,
        window_seconds: Optional[float] = None,
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

    # ---- 状态快照（用于调试/监控）----

    def to_dict(self) -> Dict[str, Any]:
        """获取状态快照（只读）"""
        with self._lock:
            return {
                "client_id": self.client_id,
                "stage": self._stage,
                "step_completed": self._step_completed,
                "last_update_time": self._last_update_time,
                "custom_state": dict(self._custom_state),
                "sequence_counters": dict(self._sequence_counters),
            }


class ClientQueues:
    """
    架构说明：
    - CA-ReadyQueue: 从 RTMP 提取的原始帧，等待推理（设置最大长度防止溢出）
    - CA-RawQueue: 原始帧副本，用于生成原始视频 HLS 段（设置最大长度防止溢出）
    - CA-ProcessedQueue: 推理后的处理帧（含关键点），用于生成处理后 HLS 段（设置最大长度防止溢出）
    - RT-ProcessedQueue: 实时推理结果（含关键点），用于 WebSocket 推送（约1秒缓存）
    
    内存保护：
    - 所有队列都设置了 maxlen 限制，当队列满时自动丢弃最旧的帧
    - 默认 CA 队列最大长度为 2700 帧，约 90 秒的视频缓存（30fps）
    - RT 队列长度约为 1 秒的帧数，用于实时推送
    
    新增功能（优化版）：
    - 支持帧率降频控制（inference_fps）
    - 支持统一 resize 尺寸配置
    - 支持直接存储 np.ndarray 原始帧
    """

    def __init__(
        self,
        client_id: str = "",
        rt_maxlen: int = 30,
        ca_segment_len: int = 150,
        ca_maxlen: int = 2700,
        resize_width: int = 640,
        resize_height: int = 480,
        inference_fps: int = 15,
        initial_stage: str = "LEAK"
    ):
        # 客户端标识
        self.client_id = client_id

        # 尺寸配置
        self.resize_width = resize_width
        self.resize_height = resize_height

        # 推理帧率配置
        self.inference_fps = inference_fps
        self.last_inference_timestamp: float = 0.0

        # 线程锁（保护时间戳更新和最新帧访问）
        self._lock = threading.Lock()

        # 最新原始帧缓存（用于异步聚合可视化）
        self.latest_raw_frame: Optional[np.ndarray] = None
        self.latest_raw_timestamp: float = 0.0

        # CA-ReadyQueue: 等待推理的原始帧（设置最大长度限制防止溢出）
        self.ca_ready: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # CA-RawQueue: 原始帧副本，用于落盘生成原始视频（设置最大长度限制）
        self.ca_raw: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # CA-ProcessedQueue: 处理后的帧，用于生成 HLS（设置最大长度限制）
        self.ca_processed: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # RT-ProcessedQueue: 实时推理结果，约 1 秒缓存用于 WebSocket 推送
        self.rt_processed: Deque[FrameData] = deque(maxlen=rt_maxlen)
        self.ca_segment_len = ca_segment_len
        self.latest_processed: Optional[FrameData] = None  # 快速访问最新处理帧
        self.task: Optional[CleaningTask] = None  # 关联的清洗任务
        self.rtmp_url: Optional[str] = None  # RTMP 流地址

        # 业务状态管理（新增）
        self.state = ClientState(client_id=client_id, initial_stage=initial_stage)

    # --- 封装操作方法，减少外部直接操作队列 ---
    def append_ca_ready(self, frame_data: FrameData) -> bool:
        """
        添加帧到待推理队列（保留兼容旧代码）
        
        Returns:
            True 表示成功，False 表示队列已满
        """
        try:
            self.ca_ready.append(frame_data)
            return True
        except Exception:
            return False
    
    def append_ca_ready_with_throttle(self, frame_data: FrameData) -> bool:
        """
        添加帧到待推理队列（带帧率限制）
        用于拉流时降频写入，避免频繁推理
        
        Args:
            frame_data: 帧数据
        
        Returns:
            True 表示写入成功，False 表示跳过（帧率限制或队列满）
        """
        with self._lock:
            # 帧率控制：间隔 = 1.0 / inference_fps
            current_time = time.time()
            interval = 1.0 / self.inference_fps
            
            if current_time - self.last_inference_timestamp < interval:
                return False  # 跳过，不写入
            
            # 检查队列是否满（maxlen 可能为 None）
            max_len = self.ca_ready.maxlen
            if max_len is not None and len(self.ca_ready) >= max_len:
                return False  # 队列满，丢弃
            
            self.ca_ready.append(frame_data)
            self.last_inference_timestamp = current_time
            return True

    def append_ca_raw(self, frame_data: FrameData) -> bool:
        """
        添加原始帧到落盘队列，同时更新最新原始帧缓存
        
        Returns:
            True 表示成功，False 表示队列已满
        """
        try:
            self.ca_raw.append(frame_data)
            # 同步更新最新原始帧（用于异步聚合可视化）
            with self._lock:
                self.latest_raw_frame = frame_data.frame
                self.latest_raw_timestamp = frame_data.timestamp
            return True
        except Exception:
            return False

    def append_ca_processed(self, frame_data: FrameData) -> None:
        self.ca_processed.append(frame_data)
        # 同步更新 latest_processed 以便快速访问
        self.latest_processed = frame_data

    def append_rt_processed(self, frame_data: FrameData) -> None:
        self.rt_processed.append(frame_data)

    def get_latest_result(self) -> Optional[FrameData]:
        """返回最新的实时结果（优先 RT 队列，否则返回 latest_processed）。"""
        if self.rt_processed:
            return self.rt_processed[-1]
        return self.latest_processed
    
    def get_latest_raw_frame(self) -> Optional[Tuple[np.ndarray, float]]:
        """安全获取最新原始帧及其时间戳。

        Returns:
            (frame, timestamp) 或 None（如果没有帧）
        """
        with self._lock:
            if self.latest_raw_frame is not None:
                return (self.latest_raw_frame.copy(), self.latest_raw_timestamp)
            return None

    def get_latest_frame(self) -> Optional[np.ndarray]:
        """获取最新原始帧（用于可视化）。

        由于推理降帧，可视化需要使用最新的原始帧而非推理时的旧帧。

        Returns:
            最新原始帧（np.ndarray）或 None（如果没有帧）
        """
        with self._lock:
            if self.latest_raw_frame is not None:
                return self.latest_raw_frame.copy()
            return None

    def get_task_id(self) -> Optional[int]:
        return self.task.task_id if self.task else None

    def to_status_dict(self) -> dict:
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "rt_processed": len(self.rt_processed),
            "rtmp_url": self.rtmp_url,
        }
    
    def get_queue_depths(self) -> dict:
        """
        获取队列深度统计（新增方法，与 to_status_dict 类似但更语义化）
        
        Returns:
            包含各队列长度的字典
        """
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "rt_processed": len(self.rt_processed),
        }

    def has_enough_for_segment(self, seg_len: int) -> bool:
        return len(self.ca_raw) >= seg_len and len(self.ca_processed) >= seg_len

    def pop_n_ca_raw(self, n: int) -> List[FrameData]:
        out: List[FrameData] = []
        for _ in range(min(n, len(self.ca_raw))):
            out.append(self.ca_raw.popleft())
        return out

    def pop_n_ca_processed(self, n: int) -> List[FrameData]:
        out: List[FrameData] = []
        for _ in range(min(n, len(self.ca_processed))):
            out.append(self.ca_processed.popleft())
        # 更新 latest_processed 为队列中最后一项（若存在），否则保持原值
        if self.ca_processed:
            self.latest_processed = self.ca_processed[-1]
        return out

    def clear(self) -> None:
        self.ca_ready.clear()
        self.ca_raw.clear()
        self.ca_processed.clear()
        self.rt_processed.clear()
        self.latest_processed = None
        self.task = None
        self.rtmp_url = None

    def pop_ca_ready(self) -> Optional[FrameData]:
        return self.ca_ready.popleft() if self.ca_ready else None

    def pop_n_ca_ready(self, n: int) -> List[FrameData]:
        out: List[FrameData] = []
        for _ in range(min(n, len(self.ca_ready))):
            out.append(self.ca_ready.popleft())
        return out

    def set_task(self, task: Optional[CleaningTask]) -> None:
        self.task = task

    def get_task(self) -> Optional[CleaningTask]:
        return self.task