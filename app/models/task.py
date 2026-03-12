from pydantic import BaseModel
from sqlalchemy import BigInteger, Boolean, Column, Integer, String, Text

from app.database import Base


class Task(BaseModel):
    """
    该模型仅用于推理过程中的任务状态跟踪，不用于数据库存储。
    """

    # 对应数据库表的字段
    task_id: int
    current_step: str  # 清洗阶段，目前只有测漏
    status: str = "paused"  # 任务状态: running, completed, cancelled, paused
    updated_at: int  # 最后更新时间，Unix timestamp

    # 通用指标
    fully_submerged: bool  # 是否完全浸没

    # 以下为测漏指标
    bending: bool  # 内镜弯曲
    bubble_detected: bool  # 气泡检测 (持续监测)
    bending_count: int = 0  # 弯折计数


# NOTE: 无代码平台托管表，_id 是平台主键(varchar)，业务主键是 task_id
class DBTask(Base):
    __tablename__ = "clean_task"

    _id = Column(String, primary_key=True)  # 平台主键 (varchar)
    cls_id = Column(String, nullable=False)  # 平台 class 标识 (NOT NULL)
    task_id = Column(BigInteger, nullable=False, index=True)  # 业务主键
    source_ip = Column(Text, index=True)
    current_step = Column(Text, default="0")
    status = Column(Text, default="paused")
    updated_time = Column(BigInteger)
    start_time = Column(BigInteger, default=0)
    end_time = Column(BigInteger, default=0)


class DBAlarm(Base):
    """告警表 ORM（只映射业务字段，忽略平台 hidden 字段）"""

    __tablename__ = "clean_alarm"

    _id = Column(String, primary_key=True)  # 平台主键 (varchar)
    alarm_id = Column(BigInteger, nullable=False, index=True)  # 业务主键
    task_id = Column(BigInteger, nullable=False, index=True)
    step_id = Column(BigInteger)
    step_name = Column(Text)
    alarm_type = Column(Text)
    severity = Column(Text)  # HTTP 上报时叫 alarm_level
    message = Column(Text)  # HTTP 上报时叫 alarm_message
    detected_at = Column(BigInteger)  # HTTP 上报时叫 alarm_time
    resolved = Column(Boolean, default=False)
    resolved_by = Column(BigInteger)
    resolved_at = Column(BigInteger)
    create_time = Column(BigInteger)  # 平台创建时间，用于排序


class TaskTracebackRequest(BaseModel):
    task_id: int
    video_type: str = "processed"  # "raw" 或 "processed"


class TaskStatusResponse(BaseModel):
    task_id: int
    status: str
    cleaning_stage: str  # current_step现在是str
    bending: bool
    bubble_detected: bool
    fully_submerged: bool
    updated_at: str
