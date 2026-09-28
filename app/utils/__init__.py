"""CleanSight 工具模块 —— 基建 leaf，谁都可以向下依赖它。

    executor     GuardedExecutor（函数级重试）
    worker_guard guarded_run（线程主循环级自愈）
    decorators   日志装饰器 log_call
    task_queue   SerialTaskQueue（单消费者串行队列）
    metrics      Prometheus 指标

**硬约束：业务代码只抛异常、不捕获**。捕获只发生在四个边界层（guarded_run /
GuardedExecutor / FastAPI 异常处理器 / main），别在业务里手写 try/except 或重试循环。

边界层分工、异常的 retryable/fatal 矩阵、重试 policy 的取值，见
docs/kb/DESIGN_FAULT_TOLERANCE.md。
"""

from .decorators import log_call
from .executor import ExecutionPolicy, GuardedExecutor
from .task_queue import SerialTaskQueue
from .worker_guard import guarded_run

__all__ = [
    # Decorators (仅用于日志)
    "log_call",
    # Executor framework (边界层异常处理)
    "GuardedExecutor",
    "ExecutionPolicy",
    # Worker guard (线程级自愈)
    "guarded_run",
    # Task queue (单消费者串行任务队列)
    "SerialTaskQueue",
]
