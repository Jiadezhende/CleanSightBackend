"""CleanSight 工具模块 —— 基建 leaf，谁都可以向下依赖它。

    executor     GuardedExecutor（函数级重试）

**硬约束：业务代码只抛异常、不捕获**。捕获只发生在四个边界层（guarded_run /
GuardedExecutor / FastAPI 异常处理器 / main），别在业务里手写 try/except 或重试循环。

边界层分工、异常的 retryable/fatal 矩阵、重试 policy 的取值，见
docs/kb/DESIGN_FAULT_TOLERANCE.md。
"""

from .executor import ExecutionPolicy, GuardedExecutor

__all__ = [
    # Executor framework (边界层异常处理)
    "GuardedExecutor",
    "ExecutionPolicy",
]
