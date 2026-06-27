from pydantic import BaseModel
from sqlalchemy import BigInteger, Boolean, Column, String, Text

from app.database import Base


class CleaningTask(BaseModel):
    """推理过程中的任务状态跟踪，不用于数据库存储。

    检测/告警状态全部走 operator → alarm 管道，不再挂在 Task 上。
    """

    task_id: int
    current_step: str  # 清洗阶段主键(step_id)，用于 stage 路由与落盘
    status: str = "paused"  # 任务状态: running, completed, cancelled, paused


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
