"""任务运行态契约。"""

from dataclasses import dataclass


@dataclass
class CleaningTask:
    """推理过程中的任务状态跟踪，不用于数据库存储（运行时 VO）。

    检测/告警状态全部走 operator → alarm 管道，不再挂在 task 上。
    """

    task_id: int
    current_step: str  # 清洗阶段主键(step_id)，用于 stage 路由与落盘
    status: str = "paused"  # running / completed / cancelled / paused
