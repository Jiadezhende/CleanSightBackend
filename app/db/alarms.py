"""`clean_alarm` 表的 SQLAlchemy ORM 映射（DB schema 单一真源）。"""

from sqlalchemy import BigInteger, Boolean, Column, String, Text

from .database import Base


class DBAlarm(Base):
    """告警表 ORM（只映射业务字段，忽略平台 hidden 字段）。"""

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
