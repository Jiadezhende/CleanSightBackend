"""CleanSight 工具模块 —— 基建 leaf，谁都可以向下依赖它。

    exceptions   AppError + 5 个服务异常 + 3 个 HTTP 业务异常；retryable/fatal 两个标志
    executor     GuardedExecutor（函数级重试）、CircuitBreaker
    worker_guard guarded_run（线程主循环级自愈）
    decorators   日志装饰器 log_call / timing
    task_queue   SerialTaskQueue（单消费者串行队列）
    metrics      Prometheus 指标
    context      task_id / client_id 的 contextvar

**硬约束：业务代码只抛异常、不捕获**。捕获只发生在四个边界层（guarded_run /
GuardedExecutor / FastAPI 异常处理器 / main），别在业务里手写 try/except 或重试循环。

边界层分工、异常的 retryable/fatal 矩阵、五个重试 policy 的取值，见
docs/kb/DESIGN_FAULT_TOLERANCE.md。
"""

from .context import (
    ClientContext,
    clear_client_id,
    clear_context,
    clear_task_id,
    get_client_id,
    get_task_id,
    set_client_id,
    set_task_id,
)
from .decorators import log_call, timing
from .exceptions import (
    AppError,
    ConflictError,
    DatabaseError,
    DirectoryGoneError,
    FFmpegError,
    ModelInferenceError,
    NotFoundError,
    PersistenceError,
    StreamConnectionError,
    ValidationError,
    is_fatal_error,
    is_retryable_error,
)
from .executor import (
    CircuitBreaker,
    ExecutionPolicy,
    GuardedExecutor,
    RetryExecutorWithCircuitBreaker,
)
from .task_queue import SerialTaskQueue
from .worker_guard import guarded_run

__all__ = [
    # Exceptions (基类 + 核心异常 + HTTP业务异常 + 工具函数)
    "AppError",
    "StreamConnectionError",
    "FFmpegError",
    "DatabaseError",
    "ModelInferenceError",
    "PersistenceError",
    "DirectoryGoneError",
    "NotFoundError",
    "ValidationError",
    "is_retryable_error",
    "is_fatal_error",
    # Decorators (仅用于日志)
    "log_call",
    "timing",
    # Executor framework (边界层异常处理)
    "GuardedExecutor",
    "CircuitBreaker",
    "RetryExecutorWithCircuitBreaker",
    "ExecutionPolicy",
    # Worker guard (线程级自愈)
    "guarded_run",
    # Task queue (单消费者串行任务队列)
    "SerialTaskQueue",
    # Context
    "set_client_id",
    "get_client_id",
    "clear_client_id",
    "set_task_id",
    "get_task_id",
    "clear_task_id",
    "clear_context",
    "ClientContext",
]
