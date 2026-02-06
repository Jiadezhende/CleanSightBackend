"""
CleanSight 装饰器工具集（仅用于日志）

包含 2 个核心装饰器：
1. log_call - 自动进入/退出日志装饰器
2. timing - 性能计时装饰器

边界层异常处理原则：
- 业务代码保持纯净，只抛异常，不捕获异常
- 重试逻辑在 RetryExecutor 框架层统一管理
- 异常捕获在 4 个边界层：Worker.run(), RetryExecutor, FastAPI handlers, main()

设计原则：
- 实用优先，避免过度设计
- 参数硬编码，零配置
- 适用于并发 <15 的小规模系统
"""

import functools
import logging
import time
import os
from typing import Callable, Optional

# 从环境变量读取调试模式
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"

# ============================================================================
# 1. 日志装饰器
# ============================================================================

def log_call(
    level: int = logging.INFO,
    log_args: bool = False,  # 是否记录参数（默认关闭，避免记录大对象）
    log_result: bool = False,  # 是否记录返回值
    log_exceptions: bool = True,  # 是否记录异常
    skip_in_production: bool = False  # 生产环境是否跳过（用于高频操作）
):
    """自动进入/退出日志装饰器

    Args:
        level: 日志级别（默认 INFO）
        log_args: 是否记录函数参数
        log_result: 是否记录返回值
        log_exceptions: 是否记录异常（包含完整 traceback）
        skip_in_production: 生产环境是否跳过（用于高频操作）

    示例:
        @log_call(level=logging.INFO, log_args=True)
        def process_frame(client_id: str, frame: np.ndarray):
            ...

        # 高频操作：生产环境跳过日志
        @log_call(skip_in_production=True)
        def infer_batch(frames):
            ...
    """
    def decorator(func: Callable):
        logger = logging.getLogger(func.__module__)

        # 生产环境跳过装饰器
        if skip_in_production and not DEBUG_MODE:
            return func

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            # 提取 client_id
            client_id = _extract_client_id(args, kwargs)
            func_name = f"{func.__module__}.{func.__name__}"

            # 构建日志消息
            log_msg = f"[ENTER] {func_name}"
            if client_id:
                log_msg += f" (client_id={client_id})"
            if log_args:
                sanitized_args = _sanitize_args(args, kwargs)
                log_msg += f" args={sanitized_args}"

            logger.log(level, log_msg)

            # 执行函数
            start_time = time.perf_counter()
            try:
                result = func(*args, **kwargs)

                # 记录退出日志
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                exit_msg = f"[EXIT] {func_name} (elapsed={elapsed_ms:.2f}ms"
                if client_id:
                    exit_msg += f", client_id={client_id}"
                if log_result:
                    exit_msg += f", result={result}"
                exit_msg += ")"
                logger.log(level, exit_msg)

                return result

            except Exception as e:
                elapsed_ms = (time.perf_counter() - start_time) * 1000

                if log_exceptions:
                    error_msg = f"[ERROR] {func_name} (elapsed={elapsed_ms:.2f}ms"
                    if client_id:
                        error_msg += f", client_id={client_id}"
                    error_msg += f", error={str(e)[:200]})"
                    logger.error(error_msg, exc_info=True)

                raise

        return wrapper
    return decorator


# ============================================================================
# 2. 性能计时装饰器
# ============================================================================

def timing(
    threshold_ms: Optional[float] = None,  # 警告阈值（毫秒）
    warn_on_slow: bool = True,  # 超过阈值时是否发出警告
    log_always: bool = False  # 是否总是记录（默认仅在超过阈值时记录）
):
    """性能计时装饰器

    Args:
        threshold_ms: 警告阈值（毫秒），超过此值时发出 WARNING
        warn_on_slow: 超过阈值时是否发出警告
        log_always: 是否总是记录性能指标

    示例:
        @timing(threshold_ms=1000.0, warn_on_slow=True)
        def infer_batch(frames):
            ...
    """
    def decorator(func: Callable):
        logger = logging.getLogger(func.__module__)

        @functools.wraps(func)
        def wrapper(*args, **kwargs):
            client_id = _extract_client_id(args, kwargs)
            start_time = time.perf_counter()

            try:
                result = func(*args, **kwargs)
                return result
            finally:
                elapsed_ms = (time.perf_counter() - start_time) * 1000
                func_name = f"{func.__module__}.{func.__name__}"

                # 构建日志消息
                timing_msg = f"[TIMING] {func_name} took {elapsed_ms:.2f}ms"
                if client_id:
                    timing_msg += f" (client_id={client_id})"

                # 检查阈值
                if threshold_ms and elapsed_ms > threshold_ms and warn_on_slow:
                    logger.warning(
                        f"[SLOW] {func_name} took {elapsed_ms:.2f}ms "
                        f"(threshold={threshold_ms}ms, client_id={client_id})"
                    )
                elif log_always:
                    logger.debug(timing_msg)

        return wrapper
    return decorator




# ============================================================================
# 工具函数
# ============================================================================

def _extract_client_id(args: tuple, kwargs: dict) -> Optional[str]:
    """从函数参数中提取 client_id

    尝试从以下位置提取：
    1. kwargs 中的 "client_id" 参数
    2. args[0] 如果是字符串（通常第一个参数是 client_id）
    3. 对象的 self.client_id 属性

    Args:
        args: 位置参数
        kwargs: 关键字参数

    Returns:
        str: client_id，如果不存在则返回 None
    """
    # 从 kwargs 提取
    if "client_id" in kwargs:
        return kwargs["client_id"]

    # 从 args[0] 提取（如果是字符串）
    if args and isinstance(args[0], str):
        return args[0]

    # 从 self.client_id 提取
    if args and hasattr(args[0], "client_id"):
        return getattr(args[0], "client_id", None)

    return None


def _sanitize_args(args: tuple, kwargs: dict) -> dict:
    """清洗参数，避免记录大对象（如 ndarray、bytes）

    Args:
        args: 位置参数
        kwargs: 关键字参数

    Returns:
        dict: 清洗后的参数字典
    """
    def sanitize_value(v):
        """清洗单个值"""
        # NumPy 数组
        try:
            import numpy as np
            if isinstance(v, np.ndarray):
                return f"<ndarray shape={v.shape} dtype={v.dtype}>"
        except ImportError:
            pass

        # 字节串
        if isinstance(v, bytes):
            return f"<bytes len={len(v)}>"

        # 大对象（字符串表示超过 200 字符）
        if hasattr(v, "__dict__"):
            obj_str = str(v)
            if len(obj_str) > 200:
                return f"<{v.__class__.__name__} object>"

        return v

    sanitized_args = tuple(sanitize_value(arg) for arg in args)
    sanitized_kwargs = {k: sanitize_value(v) for k, v in kwargs.items()}

    return {"args": sanitized_args, "kwargs": sanitized_kwargs}
