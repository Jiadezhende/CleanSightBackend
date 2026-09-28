"""
告警服务数据模型

定义告警上报任务的数据结构
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional

from app.types.alarm import ALARM_MODE_REALTIME, AlarmMetric, AlarmType


@dataclass
class AlarmReportTask:
    """告警上报任务"""

    task_id: Optional[int]
    stage: Optional[str]
    client_id: Optional[str]
    alarm_type: str
    alarm_metric: str
    alarm_mode: str
    alarm_level: str
    alarm_message: str
    step_id: Optional[int] = None
    detection_result: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "AlarmReportTask":
        """从字典创建告警任务"""
        return cls(
            task_id=data.get("task_id"),
            stage=data.get("stage"),
            step_id=data.get("step_id"),
            client_id=data.get("client_id"),
            alarm_type=data.get("alarm_type", AlarmType.PROCESS_VIOLATION),
            alarm_metric=data.get("alarm_metric", AlarmMetric.UNKNOWN),
            alarm_mode=data.get("alarm_mode", ALARM_MODE_REALTIME),
            alarm_level=data.get("alarm_level", "high"),
            alarm_message=data.get("alarm_message", "AI推理检测到异常"),
            detection_result=data.get("detection_result"),
            timestamp=time.time(),
        )

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典，供告警上报使用。"""
        return {
            "task_id": self.task_id,
            "stage": self.stage,
            "step_id": self.step_id,
            "client_id": self.client_id,
            "alarm_type": self.alarm_type,
            "alarm_metric": self.alarm_metric,
            "alarm_mode": self.alarm_mode,
            "alarm_level": self.alarm_level,
            "alarm_message": self.alarm_message,
            "detection_result": self.detection_result,
            "detected_at": self.timestamp,
        }
