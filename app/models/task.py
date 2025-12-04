from datetime import datetime
from pydantic import BaseModel
from sqlalchemy import Column, String, BigInteger, Integer
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

# TODO: 数据库已实现初始化任务的相关脚本，后端只需做更新工作
class DBTask(Base):
    __tablename__ = "clean_task"

    task_id = Column(Integer, primary_key=True, autoincrement=True)  # initialize_task设定
    source_ip = Column(String, index=True)  # initialize_task设定
    current_step = Column(String, default="0")  # 流程中断时设置，text类型
    status = Column(String, default="paused")  # 任务状态: running, completed, cancelled, paused
    updated_time = Column(BigInteger)
    start_time = Column(BigInteger, default=0)
    end_time = Column(BigInteger, default=0)

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