"""
CleanSight 重试执行器（框架边界层）

设计原则（基于《实时 AI 视觉检测项目异常处理规范》）：
- 边界层捕获异常，业务代码保持纯净
- 集中管理重试策略，避免分散在业务代码中
- 框架层统一处理，业务层只抛出异常
- 显式化 Action 决策（DROP/RETRY/FATAL）
"""

import logging
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional

from .exceptions import AppError, FrameDrop, ModelInferenceError

logger = logging.getLogger(__name__)


class Action(Enum):
    """异常处理动作枚举

    用于 Executor 决策显式化：
    - DROP: 丢弃，继续执行（用于 FrameDrop）
    - RETRY: 重试当前操作
    - FATAL: 致命错误，向上传播
    """

    DROP = "drop"  # 丢弃，继续执行（用于 FrameDrop）
    RETRY = "retry"  # 重试当前操作
    FATAL = "fatal"  # 致命错误，向上传播


@dataclass
class ExecutionPolicy:
    """重试策略配置"""

    max_attempts: int = 3
    delay: float = 1.0
    backoff: bool = False
    backoff_factor: float = 2.0
    max_delay: float = 60.0


class GuardedExecutor:
    """
    重试执行器（框架边界层）

    职责：
    1. 在框架层统一处理重试逻辑
    2. 业务代码保持纯净，只抛出异常
    3. 根据异常的 retryable 标志决定是否重试

    使用示例：
        # 业务代码（纯净，只抛异常）
        def start_ffmpeg(url: str):
            if not self._validate_url(url):
                raise StreamConnectionError(url=url, client_id=self.client_id)
            # ... FFmpeg 启动逻辑

        # 服务层调用（框架边界层处理重试）
        executor = GuardedExecutor()
        executor.execute(
            func=lambda: self.start_ffmpeg(url),
            policy_name='stream'
        )
    """

    # 预定义策略（硬编码，零配置）
    POLICIES: Dict[str, ExecutionPolicy] = {
        # 流操作：固定延迟 3 秒，最多 5 次
        "stream": ExecutionPolicy(max_attempts=5, delay=3.0, backoff=False),
        # 数据库操作：指数退避，最多 3 次
        "database": ExecutionPolicy(
            max_attempts=3, delay=1.0, backoff=True, backoff_factor=2.0, max_delay=60.0
        ),
        # 外部 API：指数退避，最多 3 次
        "external_api": ExecutionPolicy(
            max_attempts=3, delay=2.0, backoff=True, backoff_factor=2.0, max_delay=60.0
        ),
        # 模型推理：固定延迟 1 秒，最多 2 次
        "inference": ExecutionPolicy(max_attempts=2, delay=1.0, backoff=False),
        # 持久化操作：指数退避，最多 3 次
        "persistence": ExecutionPolicy(
            max_attempts=3, delay=1.0, backoff=True, backoff_factor=2.0, max_delay=30.0
        ),
    }

    def __init__(self, custom_policies: Optional[Dict[str, ExecutionPolicy]] = None):
        """
        初始化执行器

        Args:
            custom_policies: 自定义策略字典（可选）
        """
        self.policies = {**self.POLICIES}
        if custom_policies:
            self.policies.update(custom_policies)

    def execute(
        self,
        func: Callable[[], Any],
        policy_name: str = "database",
        on_retry: Optional[Callable[[int, Exception], None]] = None,
    ) -> Any:
        """
        执行带重试的操作（框架边界层）

        基于《实时 AI 视觉检测项目异常处理规范》：
        - 根据异常类型和 Policy 决策处理动作（DROP/RETRY/FATAL）
        - FrameDrop 异常走 DROP 路径，返回 None
        - 强制记录 metrics（成功/失败）

        Args:
            func: 要执行的函数（无参数，使用 lambda 或闭包传递参数）
            policy_name: 策略名称（'stream', 'database', 'external_api', etc.）
            on_retry: 重试回调函数 (attempt, exception) -> None

        Returns:
            函数执行结果，或 None（FrameDrop 时）

        Raises:
            AppError: 致命错误或重试耗尽
            Exception: 未知异常

        示例：
            # 业务代码（纯净）
            def connect_stream(url: str):
                # 只抛异常，不处理重试
                if error:
                    raise StreamConnectionError(url=url)

            # 框架边界调用
            executor.execute(
                func=lambda: connect_stream(url),
                policy_name='stream'
            )
        """
        policy = self.policies.get(policy_name)
        if not policy:
            raise ValueError(f"Unknown policy: {policy_name}")

        attempts = 0

        while attempts < policy.max_attempts:
            try:
                # 执行业务逻辑（可能抛出异常）
                result = func()

                # 成功：记录 metrics
                self._record_success(policy_name, attempts)
                return result

            except AppError as e:
                attempts += 1

                # 决策动作
                action = self._decide_action(e, policy, attempts)

                # 记录 metrics（强制）
                self._record_exception(policy_name, e, action, attempts)

                if action == Action.DROP:
                    # 安静丢弃（FrameDrop）
                    logger.debug(f"[GuardedExecutor] Dropped: {e}")
                    return None

                elif action == Action.RETRY:
                    # 重试前日志
                    delay = self._calculate_delay(policy, attempts)
                    logger.warning(
                        f"[GuardedExecutor] Retry {attempts}/{policy.max_attempts} "
                        f"after {delay:.2f}s (policy={policy_name}, error={str(e)[:100]})"
                    )

                    # 调用回调
                    if on_retry:
                        on_retry(attempts, e)

                    # 等待后重试
                    time.sleep(delay)
                    continue

                elif action == Action.FATAL:
                    # 致命错误，向上传播
                    logger.error(
                        f"[GuardedExecutor] Fatal error (policy={policy_name}): {e}",
                        exc_info=True,
                    )
                    raise

            except Exception as e:
                # 未知异常 -> 致命
                logger.critical(
                    f"[GuardedExecutor] Unhandled exception (policy={policy_name}): {e}",
                    exc_info=True,
                )
                self._record_exception(policy_name, e, Action.FATAL, attempts)
                raise

        # 重试耗尽 -> 致命
        logger.error(f"[GuardedExecutor] Max attempts reached for {policy_name}")
        raise

    def _decide_action(
        self, exc: AppError, policy: ExecutionPolicy, attempts: int
    ) -> Action:
        """
        决策处理动作（核心决策逻辑）

        基于《实时 AI 视觉检测项目异常处理规范》：
        - FrameDrop -> DROP（安静丢弃）
        - fatal=True -> FATAL（致命错误）
        - retryable=True 且未超过次数 -> RETRY（重试）
        - 其他 -> FATAL（向上传播）

        Args:
            exc: AppError 异常实例
            policy: 重试策略
            attempts: 当前尝试次数

        Returns:
            Action: 处理动作（DROP/RETRY/FATAL）
        """
        # 1. FrameDrop -> DROP
        if isinstance(exc, FrameDrop):
            return Action.DROP

        # 2. fatal=True -> FATAL
        if exc.fatal:
            return Action.FATAL

        # 3. retryable=True 且未超过次数 -> RETRY
        if exc.retryable and attempts < policy.max_attempts:
            return Action.RETRY

        # 4. 其他 -> FATAL
        return Action.FATAL

    def _calculate_delay(self, policy: ExecutionPolicy, attempts: int) -> float:
        """
        计算重试延迟

        Args:
            policy: 重试策略
            attempts: 当前尝试次数

        Returns:
            float: 延迟时间（秒）
        """
        if policy.backoff:
            # 指数退避
            delay = min(
                policy.delay * (policy.backoff_factor ** (attempts - 1)),
                policy.max_delay,
            )
        else:
            # 固定延迟
            delay = policy.delay
        return delay

    def _record_success(self, policy_name: str, attempts: int):
        """
        记录成功 metrics

        Args:
            policy_name: 策略名称
            attempts: 尝试次数
        """
        # 如果有重试，记录到 metrics
        if attempts > 0:
            from app.utils.metrics import retry_total

            retry_total.labels(operation=policy_name, error_type="recovered").inc()

    def _record_exception(
        self, policy_name: str, exc: Exception, action: Action, attempts: int
    ):
        """
        记录异常 metrics

        Args:
            policy_name: 策略名称
            exc: 异常实例
            action: 处理动作
            attempts: 尝试次数
        """
        from app.utils.metrics import frame_drop_total, gpu_oom_total, retry_total

        exc_type = type(exc).__name__

        # 1. 通用重试计数（所有异常）
        if action in (Action.RETRY, Action.FATAL):
            retry_total.labels(operation=policy_name, error_type=exc_type).inc()

        # 2. FrameDrop 专用计数
        if isinstance(exc, FrameDrop):
            frame_drop_total.labels(reason=exc.reason or "unknown").inc()

        # 3. GPU OOM 专用计数
        if isinstance(exc, ModelInferenceError) and exc.is_cuda_error:
            gpu_oom_total.labels(model=exc.model_name or "unknown").inc()


class CircuitBreaker:
    """
    熔断器（框架边界层）

    职责：
    1. 保护下游服务（数据库、外部 API）
    2. 连续失败 N 次后打开熔断器，快速失败
    3. 超时后自动尝试恢复

    使用示例：
        # 创建熔断器
        db_breaker = CircuitBreaker(
            name='database',
            max_failures=5,
            reset_timeout=60.0
        )

        # 业务代码（纯净）
        def query_task(task_id: int):
            task = db.query(Task).filter_by(id=task_id).first()
            if not task:
                raise DatabaseError(f"Task {task_id} not found")
            return task

        # 框架边界调用
        db_breaker.call(lambda: query_task(task_id))
    """

    def __init__(self, name: str, max_failures: int = 5, reset_timeout: float = 60.0):
        """
        初始化熔断器

        Args:
            name: 熔断器名称（用于日志）
            max_failures: 连续失败阈值
            reset_timeout: 重置超时时间（秒）
        """
        self.name = name
        self.max_failures = max_failures
        self.reset_timeout = reset_timeout

        # 状态
        self.failure_count = 0
        self.last_failure_time = 0.0
        self.is_open = False

    def call(self, func: Callable[[], Any]) -> Any:
        """
        通过熔断器执行函数

        Args:
            func: 要执行的函数

        Returns:
            函数执行结果

        Raises:
            Exception: 熔断器打开时抛出
            原始异常: 执行失败时抛出
        """
        current_time = time.time()

        # 检查是否应该重置熔断器
        if self.is_open:
            if (current_time - self.last_failure_time) > self.reset_timeout:
                logger.info(
                    f"[CircuitBreaker] {self.name} reset after {self.reset_timeout}s"
                )
                self.is_open = False
                self.failure_count = 0
            else:
                # 熔断器打开，快速失败
                raise Exception(
                    f"Circuit breaker '{self.name}' is OPEN "
                    f"(failures={self.failure_count}/{self.max_failures})"
                )

        try:
            # 执行业务逻辑
            result = func()

            # 成功，重置失败计数
            if self.failure_count > 0:
                logger.info(
                    f"[CircuitBreaker] {self.name} success, reset failure count"
                )
            self.failure_count = 0

            return result

        except Exception as e:
            # 失败，增加计数
            self.failure_count += 1
            self.last_failure_time = current_time

            logger.warning(
                f"[CircuitBreaker] {self.name} failure {self.failure_count}/{self.max_failures}: "
                f"{str(e)[:100]}"
            )

            # 达到阈值，打开熔断器
            if self.failure_count >= self.max_failures:
                self.is_open = True
                logger.error(
                    f"[CircuitBreaker] {self.name} is now OPEN "
                    f"(failures={self.failure_count}/{self.max_failures})"
                )

            raise


class RetryExecutorWithCircuitBreaker:
    """
    带熔断器的重试执行器（框架边界层）

    组合 GuardedExecutor 和 CircuitBreaker，适用于数据库和外部 API

    使用示例：
        # 创建执行器
        db_executor = RetryExecutorWithCircuitBreaker(
            policy_name='database',
            breaker_name='database',
            max_failures=5,
            reset_timeout=60.0
        )

        # 业务代码（纯净）
        def query_task(task_id: int):
            task = db.query(Task).filter_by(id=task_id).first()
            if not task:
                raise DatabaseError(f"Task {task_id} not found")
            return task

        # 框架边界调用（重试 + 熔断器）
        db_executor.execute(lambda: query_task(task_id))
    """

    def __init__(
        self,
        policy_name: str = "database",
        breaker_name: str = "default",
        max_failures: int = 5,
        reset_timeout: float = 60.0,
    ):
        """
        初始化执行器

        Args:
            policy_name: 重试策略名称
            breaker_name: 熔断器名称
            max_failures: 熔断器失败阈值
            reset_timeout: 熔断器重置超时
        """
        self.retry_executor = GuardedExecutor()
        self.circuit_breaker = CircuitBreaker(
            name=breaker_name, max_failures=max_failures, reset_timeout=reset_timeout
        )
        self.policy_name = policy_name

    def execute(
        self,
        func: Callable[[], Any],
        on_retry: Optional[Callable[[int, Exception], None]] = None,
    ) -> Any:
        """
        执行带重试和熔断器的操作

        Args:
            func: 要执行的函数
            on_retry: 重试回调函数

        Returns:
            函数执行结果

        Raises:
            原始异常或熔断器异常
        """
        # 先检查熔断器，再执行重试
        return self.circuit_breaker.call(
            lambda: self.retry_executor.execute(
                func=func, policy_name=self.policy_name, on_retry=on_retry
            )
        )
