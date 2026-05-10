"""
持久化数据模型

定义持久化任务的数据结构
"""

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from app.models.frame import FrameData
from app.services.inference.data_models import ALARM_MODE_REALTIME, AlarmMetric, AlarmType


@dataclass
class HLSPersistenceTask:
    """HLS视频段持久化任务

    存储分区键为 (task_id, step_id)，与运行时 client_id（即 source_ip）解耦。
    """

    task_id: int
    step_id: int
    segment_type: str  # "raw" or "processed"
    frames: List[FrameData]
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        assert self.segment_type in [
            "raw",
            "processed",
        ], f"Invalid segment_type: {self.segment_type}"
        assert len(self.frames) > 0, "frames list cannot be empty"


@dataclass
class AlarmPersistenceTask:
    """告警持久化任务"""

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
    def from_dict(cls, data: Dict[str, Any]) -> "AlarmPersistenceTask":
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


@dataclass
class PersistenceMetrics:
    """持久化指标"""

    hls_enqueued: int = 0
    hls_completed: int = 0
    hls_errors: int = 0
    hls_queue_full: int = 0

    alarm_enqueued: int = 0
    alarm_completed: int = 0
    alarm_errors: int = 0
    alarm_queue_full: int = 0
