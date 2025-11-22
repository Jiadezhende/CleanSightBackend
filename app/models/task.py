from datetime import datetime
from pydantic import BaseModel
from sqlalchemy import Column, String, Integer, DateTime
from app.database import Base

class Task(BaseModel):
    # 对应数据库表的字段
    task_id: int
    initiator_operator_id: int # 关联的执行者 ID
    current_step: int  # 清洗阶段 (e.g., 1, 2, 3)
    status: str = "active"  # 任务状态: active, completed, failed
    created_at: datetime = datetime.now()

    # 以下为自定义指标
    bending_count: int  # 内镜弯曲次数
    bubble_detected: bool  # 气泡检测 (持续监测)
    fully_submerged: bool  # 是否完全浸没
    updated_at: datetime = datetime.now()

class DBTask(Base):
    __tablename__ = "clean_task"

    task_id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    initiator_operator_id = Column(Integer, index=True)
    current_step = Column(String, default=0)
    status = Column(String, default="active")
    created_at = Column(DateTime, default=datetime.now)