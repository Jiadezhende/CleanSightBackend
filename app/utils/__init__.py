"""
CleanSight 工具模块（边界层异常处理架构）

包含：
- exceptions: 自定义异常层次结构（5 个核心异常类）
- decorators: 日志装饰器（log_call、timing）
- executor: 框架边界层（RetryExecutor、CircuitBreaker）
- context: 简单上下文管理

核心原则：
- 业务代码保持纯净：只抛异常，不捕获异常
- 重试逻辑在框架边界层：使用 RetryExecutor
- 异常捕获在 4 个边界层：Worker.run(), RetryExecutor, FastAPI handlers, main()

详细文档：app/utils/BOUNDARY_LAYER_EXAMPLES.md
"""

from .exceptions import (
    CleanSightException,
    StreamConnectionError,
    FFmpegError,
    DatabaseError,
    ModelInferenceError,
    PersistenceError,
    is_retryable_error,
    get_client_id_from_exception,
)

from .decorators import (
    log_call,
    timing,
)

from .executor import (
    RetryExecutor,
    CircuitBreaker,
    RetryExecutorWithCircuitBreaker,
    RetryPolicy,
)

from .context import (
    set_client_id,
    get_client_id,
    clear_client_id,
    set_task_id,
    get_task_id,
    clear_task_id,
    clear_context,
    ClientContext,
)

__all__ = [
    # Exceptions
    "CleanSightException",
    "StreamConnectionError",
    "FFmpegError",
    "DatabaseError",
    "ModelInferenceError",
    "PersistenceError",
    "is_retryable_error",
    "get_client_id_from_exception",
    # Decorators (仅用于日志)
    "log_call",
    "timing",
    # Executor framework (边界层异常处理)
    "RetryExecutor",
    "CircuitBreaker",
    "RetryExecutorWithCircuitBreaker",
    "RetryPolicy",
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
