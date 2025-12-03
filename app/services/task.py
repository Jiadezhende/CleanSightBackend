"""
任务控制，包括初始化、终止任务等功能
以及任务状态异常处理
"""
from typing import Optional
import time
from app.models.frame import HLSSegment
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
    now_ts = int(time.time())

    try:
        # 创建数据库任务记录（不提供 task_id，让数据库自增生成）
        db_task = DBTask(
            initiator_operator_id=actor_id,
            current_step="0",
            status="initialized",
            created_at=now_ts,  # Unix timestamp
            updated_at=now_ts,
            start_time=now_ts,
        )
        
        # 添加到会话并提交到数据库，生成自增 ID
        db.add(db_task)
        db.commit()
        db.refresh(db_task)  # 刷新以获取生成的 task_id
        
        # 现在 db_task.task_id 有值，使用它创建 CleaningTask
        task = CleaningTask(
            task_id=int(db_task.task_id),  # type: ignore  # 运行时是 int，类型检查器误认为 Column
            initiator_operator_id=actor_id,
            current_step="0",
            bending_count=0,
            bubble_detected=False,
            fully_submerged=False,
            status="initialized",
            created_at=now_ts,
            updated_at=now_ts,
            start_time=0,  # 初始为0，表示未开始
            end_time=0,    # 初始为0，表示未结束
        )
        
        # 返回任务对象
        return task
    except Exception as e:
        db.rollback()  # 出错时回滚
        raise e
    finally:
        db.close()  # 确保会话关闭

def start_task(task: Optional[CleaningTask]) -> bool:
    """
    开始任务，设置start_time
    """
    if task is None:
        return False

    db = next(get_db())
    now_ts = int(time.time())
    try:
        db_task = db.query(DBTask).filter(DBTask.task_id == task.task_id).first()
        if db_task is None:
            return False
        
        db_task.start_time = now_ts  # type: ignore
        db_task.status = "running"  # type: ignore
        db_task.updated_at = now_ts  # type: ignore
        
        db.commit()
        
        # 更新内存中的task对象
        task.start_time = now_ts
        task.status = "running"
        task.updated_at = now_ts
        
        return True
    except Exception as e:
        db.rollback()
        return False
    finally:
        db.close()

def terminate_task(task: Optional[CleaningTask]) -> bool:
    """
    中断/终止任务
    """
    if task is None:
        return False

    # 使用 get_db() 获取会话
    db = next(get_db())
    now_ts = int(time.time())
    try:
        # 根据 task_id 查询数据库中的 DBTask
        db_task = db.query(DBTask).filter(DBTask.task_id == task.task_id).first()
        if db_task is None:
            return False
        
        # 更新数据库任务状态
        db_task.status = "terminated"  # type: ignore
        db_task.end_time = now_ts  # type: ignore
        db_task.updated_at = now_ts  # type: ignore
        
        # 提交更改
        db.commit()
        
        # 更新内存中的task对象
        task.status = "terminated"
        task.end_time = now_ts
        task.updated_at = now_ts
        
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

def get_task_traceback(task_id: int) -> Optional[str]:
    """
    根据 task_id 查询 HLS 播放列表路径，返回任务的完整清洗视频追溯。
    
    Args:
        task_id: 任务 ID
        
    Returns:
        HLS 播放列表文件路径，如果不存在则返回 None
    """
    db = next(get_db())
    try:
        # 查询该任务的所有 HLS 段，按时间排序
        segments = db.query(HLSSegment).filter(HLSSegment.task_id == task_id).order_by(HLSSegment.start_ts).all()
        if not segments:
            return None
        # 返回播放列表路径（假设所有段共享同一个播放列表）
        return segments[0].playlist_path  # type: ignore
    finally:
        db.close()
