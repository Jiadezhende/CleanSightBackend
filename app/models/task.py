from datetime import datetime
from pydantic import BaseModel
from sqlalchemy import Column, String, BigInteger, Integer
from app.database import Base

class Task(BaseModel):
    # 对应数据库表的字段
    task_id: int
    initiator_operator_id: int  # 关联的执行者 ID
    current_step: str  # 清洗阶段 (e.g., "1", "2", "3")，流程中断时设置
    status: str = "active"  # 任务状态: active, completed, failed
    created_at: int  # 任务创建时间(initialize_task设定)，Unix timestamp
    updated_at: int  # 更新时间
    start_time: int  # 开始时间
    end_time: int  # 结束时间

    # 以下为自定义指标
    bending_count: int  # 内镜弯曲次数
    bubble_detected: bool  # 气泡检测 (持续监测)
    fully_submerged: bool  # 是否完全浸没

class DBTask(Base):
    __tablename__ = "clean_task"

    task_id = Column(Integer, primary_key=True, autoincrement=True)  # initialize_task设定
    initiator_operator_id = Column(BigInteger, index=True)  # initialize_task设定
    current_step = Column(String, default="0")  # 流程中断时设置，text类型
    status = Column(String, default="active")
    created_at = Column(BigInteger)  # 任务创建时间(initialize_task设定)，Unix timestamp
    updated_at = Column(BigInteger)
    start_time = Column(BigInteger, default=0)
    end_time = Column(BigInteger, default=0)