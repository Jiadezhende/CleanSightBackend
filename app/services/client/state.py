"""
客户端业务状态管理
"""

from collections import deque
from typing import Deque, Optional, List, Tuple, Any, Dict
import time
import threading


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
