"""`clean_task` 表的 SQLAlchemy ORM 映射（DB schema 单一真源）。"""

from sqlalchemy import BigInteger, Column, String, Text

from .database import Base


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
