"""
任务控制，包括初始化、终止任务等功能
以及任务状态异常处理
"""
from typing import Optional
from app.models.task import Task as CleaningTask
from datetime import datetime

def initialize_task(actor_id: str) -> CleaningTask:
    """
    初始化清洗任务
    """
    task = CleaningTask(
        task_id="generated_task_id",  # TODO: DB操作，返回唯一任务ID
        actor_id=actor_id,
        cleaning_stage=0,
        bending_count=0,
        bubble_detected=False,
        fully_submerged=False
    )
    return task

def terminate_task(task: Optional[CleaningTask]) -> bool:
    """
    中断/终止任务
    """
    if task is None:
        return False

    task.status = "terminated"
    task.updated_at = datetime.now()
    # TODO: 清理相关资源，保存到数据库等
    return True

def task_exception(task: Optional[CleaningTask]):
    """
    检测任务异常
    比如进入阶段2但是测漏阶段未弯曲软管
    """
    if task is None:
        return
    # TODO
