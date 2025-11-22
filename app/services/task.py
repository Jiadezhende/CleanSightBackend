"""
任务控制，包括初始化、终止任务等功能
以及任务状态异常处理
"""
from typing import Optional
from app.models.task import Task as CleaningTask, DBTask
from datetime import datetime
from app.database import get_db
import uuid

def initialize_task(actor_id: int) -> CleaningTask:
    """
    初始化清洗任务
    """
    # 首先在数据库中创建任务记录
    db = next(get_db())

    try:
        # 创建数据库任务记录（不提供 task_id，让数据库自增生成）
        db_task = DBTask(
            initiator_id=actor_id,
            current_step=0,
            status="initialized",
            created_at=datetime.now()
        )
        
        # 添加到会话并提交到数据库，生成自增 ID
        db.add(db_task)
        db.commit()
        db.refresh(db_task)  # 刷新以获取生成的 task_id
        
        # 现在 db_task.task_id 有值，使用它创建 CleaningTask
        task = CleaningTask(
            task_id=int(db_task.task_id),  # type: ignore  # 运行时是 int，类型检查器误认为 Column
            initiator_operator_id=actor_id,
            current_step=0,
            bending_count=0,
            bubble_detected=False,
            fully_submerged=False,
            status="initialized",
            created_at=datetime.now(),
            updated_at=datetime.now()
        )
        
        # 返回任务对象
        return task
    except Exception as e:
        db.rollback()  # 出错时回滚
        raise e
    finally:
        db.close()  # 确保会话关闭

def terminate_task(task: Optional[CleaningTask]) -> bool:
    """
    中断/终止任务
    """
    if task is None:
        return False

    # 使用 get_db() 获取会话
    db = next(get_db())
    try:
        # 根据 task_id 查询数据库中的 DBTask
        db_task = db.query(DBTask).filter(DBTask.task_id == task.task_id).first()
        if db_task is None:
            return False
        
        # 更新数据库任务状态
        db_task.status = "terminated"  # type: ignore
        
        # 提交更改
        db.commit()
        
        # TODO: 清理相关资源
        return True
    except Exception as e:
        db.rollback()
        return False
    finally:
        db.close()

def task_exception(task: Optional[CleaningTask]):
    """
    检测任务异常
    比如进入阶段2但是测漏阶段未弯曲软管
    """
    if task is None:
        return
    # TODO
