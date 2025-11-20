from datetime import datetime
from pydantic import BaseModel

class Task(BaseModel):
    task_id: str
    actor_id: str # 关联的执行者 ID
    cleaning_stage: int  # 清洗阶段 (e.g., 1, 2, 3)
    bending_count: int  # 内镜弯曲次数
    bubble_detected: bool  # 气泡检测 (持续监测)
    fully_submerged: bool  # 是否完全浸没
    status: str = "active"  # 任务状态: active, completed, failed
    created_at: datetime = datetime.now()
    updated_at: datetime = datetime.now()