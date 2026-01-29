"""
持久化数据模型

定义持久化任务的数据结构
"""

from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
import time

from app.models.frame import FrameData


@dataclass
class HLSPersistenceTask:
    """HLS视频段持久化任务"""
    client_id: str
    task_id: int
    segment_type: str  # "raw" or "processed"
    frames: List[FrameData]
    timestamp: float = field(default_factory=time.time)

    def __post_init__(self):
        assert self.segment_type in ["raw", "processed"], f"Invalid segment_type: {self.segment_type}"
        assert len(self.frames) > 0, "frames list cannot be empty"


@dataclass
class AlarmPersistenceTask:
    """告警持久化任务"""
    task_id: Optional[int]
    step_id: Optional[int]
    client_id: Optional[str]
    alarm_type: str
    alarm_level: str
    alarm_message: str
    detection_result: Optional[Dict[str, Any]] = None
    timestamp: float = field(default_factory=time.time)

    # 聚合字段（由告警处理器填充）
    alarm_count: Optional[int] = None
    first_seen: Optional[str] = None
    last_seen: Optional[str] = None

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> 'AlarmPersistenceTask':
        """从字典创建告警任务"""
        return cls(
            task_id=data.get('task_id'),
            step_id=data.get('step_id'),
            client_id=data.get('client_id'),
            alarm_type=data.get('alarm_type', '流程违规'),
            alarm_level=data.get('alarm_level', 'high'),
            alarm_message=data.get('alarm_message', 'AI推理检测到异常'),
            detection_result=data.get('detection_result'),
            timestamp=time.time()
        )

    def get_key(self) -> str:
        """生成去重键（用于批量去重）"""
        return f"{self.task_id}_{self.step_id}"


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
