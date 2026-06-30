"""时序分析层 (L3/L4)。

抽象：Operator / AlignedFrame（operator.py，合并 analyze 推进状态 + judge 出告警）
处理流程：ClientTemporalActor 每 Client 1Hz tick（actor.py）→ persist_alarms L4 出口（alarm_sink.py）
"""

from .operator import AlignedFrame, Operator
from .alarm_sink import persist_alarms
from .actor import ClientTemporalActor

__all__ = [
    "Operator",
    "AlignedFrame",
    "ClientTemporalActor",
    "persist_alarms",
]
