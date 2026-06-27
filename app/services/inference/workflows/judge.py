"""Judge — 规则基类（L4：消费事实出告警）

L4 规则层消费 L3 产出的事实（EventFact / SegmentFact），产生告警 / 离线分析结果，
说明违规点，交由 ClientTemporalActor 落盘上报。

职责边界：
- 阈值 / required 等判定参数归 Judge 自身字段（不进 fact.meta）。
- 决策状态（上升沿锁存 alarming、结算比较）是 Judge 内部成员 self._sm。
- 告警的路由指标(metric)由 Judge 在构造 AlarmInfo 时显式填(它本就知道自己代表哪个指标)；
  告警消息文本仅供展示，可自由改，不参与路由。

消费形态（本轮 P0）：
- 一 Judge 配一 Analyzer，step(facts) 直接读本 analyzer 当 tick 的 List[EventFact]。
- 入口先建 tick 内快照 frame = {f.signal: f for f in facts}，之后按信号名随机访问
  （单个 analyzer 天然多信号，如 bending 同 tick 出 state+count）。
- 本轮 frame 不跨 tick 持久；待出现「跨 analyzer / 信号错 tick 到达」时再升级为
  持久 latest-wins frame（见 specs / 计划文档的方向约定）。

约束：派生信号顶回 L3、rule 不读 rule —— Judge 只读 frame，不互相依赖。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple

from app.services.inference.data_models import AlarmInfo, EventFact

logger = logging.getLogger(__name__)


class Judge(ABC):
    """规则基类：消费事实，产实时告警 / 结算告警。

    约束：
    - self.name 与配对 Analyzer.name 一致。
    - self._sm 为决策状态机（上升沿锁存等），由子类初始化。
    - 每个 Client 独立实例，不跨 Client 共享。
    """

    def __init__(self, name: str):
        self.name = name
        self._sm: Dict[str, Any] = {}

    @staticmethod
    def _frame(facts: List[EventFact]) -> Dict[str, EventFact]:
        """把本 tick 的 facts 建成 {signal: fact} 快照（单次 O(facts)，之后按名随机访问）。"""
        return {f.signal: f for f in facts}

    @abstractmethod
    def step(self, facts: List[EventFact]) -> Tuple[List[str], List[AlarmInfo]]:
        """实时：消费本 tick 事实，上升沿触发告警。

        Returns:
            (events, alarms)
            - events: 前端展示文字事件（经 VisualizationWorker 渲染为 overlay）
            - alarms: 实时告警（经 persistence_manager 批量上报）
        """

    def finalize(self) -> List[AlarmInfo]:
        """结算：任务 terminate 时调用一次，产出结算告警。默认返空。"""
        return []
