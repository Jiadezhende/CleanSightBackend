"""
CleanSight 工具模块（边界层异常处理架构）

包含：
- exceptions: 自定义异常层次结构（AppError + 5 个核心异常）
- decorators: 日志装饰器（log_call、timing）
- executor: 框架边界层（GuardedExecutor、CircuitBreaker）
- metrics: Prometheus 可观测性指标
- context: 简单上下文管理

核心原则：
- 业务代码保持纯净：只抛异常，不捕获异常
- 重试逻辑在框架边界层：使用 GuardedExecutor
- 异常捕获在 4 个边界层：Worker.run(), GuardedExecutor, FastAPI handlers, main()

详细文档：app/utils/BOUNDARY_LAYER_EXAMPLES.md
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
from .worker_guard import guarded_run

__all__ = [
    # Exceptions (基类 + 核心异常 + HTTP业务异常 + 工具函数)
    "AppError",
    "StreamConnectionError",
    "FFmpegError",
    "DatabaseError",
    "ModelInferenceError",
    "PersistenceError",
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
