"""
任务控制，包括初始化、终止任务等功能
以及任务状态异常处理
"""
from typing import Optional
from app.models.frame import HLSSegment
from app.models.task import Task as CleaningTask
from app.database import get_db

# ai服务中不需要实现初始化功能，只需要start/terminate功能
# 原有initalize_task已弃用

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
