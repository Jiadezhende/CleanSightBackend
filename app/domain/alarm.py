"""告警契约（L4 规则层产出）。"""

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict


class AlarmType(str, Enum):
    """告警类型枚举 — value 为外部持久化使用的中文字符串。"""

    PROCESS_VIOLATION = "流程违规"
    TASK_TIMEOUT = "任务超时"
    MOCK = "mock_alarm"  # 仅测试用


class AlarmMetric(str, Enum):
    """告警指标枚举 — value 为前端展示与持久化使用的字符串。"""

    BUBBLE = "BUBBLE"
    BENDING = "BENDING"
    TASK_TIMEOUT = "TASK_TIMEOUT"
    UNKNOWN = "UNKNOWN"


# 告警模式常量
ALARM_MODE_REALTIME = "REALTIME"
ALARM_MODE_SETTLEMENT = "SETTLEMENT"


@dataclass
class Alarm:
    """告警：由 Operator.judge()（实时上升沿）/ finalize()（结算）产出。

    核心字段（alarm_type/level/message/metric/metadata）由产出方（Operator）填；
    mode/stage/seq/timestamp 在落 alarm_log 时由 alarm_sink / 环形缓冲补全（故带默认值）。
    metric 由产出方显式填，下游持久化直接读 alarm.metric，不靠文案反推。
    （原 AlarmRecord 已并入此类——同一份告警从产出到落缓冲只有一种形态。）
    """

    alarm_type: AlarmType  # 告警类型
    alarm_level: str  # "low" / "medium" / "high" / "critical"
    alarm_message: str  # 告警消息
    metric: AlarmMetric = AlarmMetric.UNKNOWN  # 路由指标，产出方显式填
    metadata: Dict[str, Any] = field(default_factory=dict)  # 落库 detection_result（触发证据）
    # ↓ 落 alarm_log 时补全
    mode: str = ALARM_MODE_REALTIME
    stage: str = ""
    seq: int = 0
    timestamp: float = field(default_factory=time.time)
