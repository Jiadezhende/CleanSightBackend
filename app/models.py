"""SQLAlchemy ORM（DB 行映射，DB schema 单一真源）。

只放 ORM；运行时契约见 app/domain，API DTO 跟各自 router 走。
"""

from sqlalchemy import BigInteger, Boolean, Column, String, Text

from app.database import Base


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
