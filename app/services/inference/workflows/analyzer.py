"""TemporalAnalyzer — 有状态时序分析基类

每个 Client 独立实例化一个 TemporalAnalyzer，运行于 ClientTemporalActor 的时序线程（1Hz）。

设计原则：
- 状态机 self._sm 是 TemporalAnalyzer 的成员变量，由子类在 __init__ 中初始化
- analyze_temporal(window) 不接受外部 sm 参数，直接操作 self._sm
- 同一 TemporalAnalyzer 实例不跨 Client 共享（每个 Client 调用 set_task() 时创建新实例）
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple

from app.services.inference.data_models import AlarmInfo, DetectionOutput

logger = logging.getLogger(__name__)


class TemporalAnalyzer(ABC):
    """有状态时序分析器基类。

    职责：
    - 持有 per-client 状态机 self._sm（ByteTrack 实例、计数器、告警锁存等）
    - 在 1Hz tick 中接收滑动窗口快照，更新状态机，产出事件和告警
    - 在任务结束时产出结算告警

    约束：
    - self.name 必须与配对 Detector.name 一致，用于 slide_window 查找
    - self._sm 由子类 __init__ 负责初始化
    - 每个 Client 独立实例，不跨 Client 共享
    """

    def __init__(self, name: str):
        self.name = name
        self._sm: Dict[str, Any] = {}  # 子类 __init__ 负责完整初始化

    @abstractmethod
    def analyze_temporal(
        self,
        window: List[DetectionOutput],
    ) -> Tuple[List[str], List[AlarmInfo]]:
        """时序分析（含去抖 + 告警评估）。

        流程：
        1. 从 window 提取新帧（使用 self._sm 中的游标跳过已处理帧）
        2. 更新 self._sm 中的计数器/追踪器
        3. 计算指标（birth_rate / bend_actions 等）
        4. 上升沿触发时产出 AlarmInfo；持续触发不重复产出

        Args:
            window: 滑动窗口快照（DetectionOutput 列表，按时间升序）
                    由 ClientTemporalActor 从 cq.get_slide_window(self.name) 获取

        Returns:
            (events, alarms)
            - events: 前端展示的文字事件（经 VisualizationWorker 渲染为 overlay）
            - alarms: 持久化告警（经 persistence_manager 批量上报）
        """

    def finalize(self) -> List[AlarmInfo]:
        """结算告警：任务 terminate 时由 ClientTemporalActor 调用一次。

        基于 self._sm 中的累计指标评估最终结果。
        默认实现返回空列表（无结算告警）。
        子类按需 override（如弯曲不足时发出 warning）。

        Returns:
            List[AlarmInfo]：结算告警列表
        """
        return []
