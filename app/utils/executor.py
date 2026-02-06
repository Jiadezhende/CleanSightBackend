"""
CleanSight 重试执行器（框架边界层）

设计原则：
- 边界层捕获异常，业务代码保持纯净
- 集中管理重试策略，避免分散在业务代码中
- 框架层统一处理，业务层只抛出异常
"""

import time
import logging
from typing import Callable, Any, Optional, Dict
from dataclasses import dataclass

from .exceptions import CleanSightException, is_retryable_error

logger = logging.getLogger(__name__)


@dataclass
class RetryPolicy:
    """重试策略配置"""
    max_attempts: int = 3
    delay: float = 1.0
    backoff: bool = False
    backoff_factor: float = 2.0
    max_delay: float = 60.0


class RetryExecutor:
    """
    重试执行器（框架边界层）

    职责：
    1. 在框架层统一处理重试逻辑
    2. 业务代码保持纯净，只抛出异常
    3. 根据异常的 retry_able 标志决定是否重试

    使用示例：
        # 业务代码（纯净，只抛异常）
        def start_ffmpeg(url: str):
            if not self._validate_url(url):
                raise StreamConnectionError(url=url, client_id=self.client_id)
            # ... FFmpeg 启动逻辑

        # 服务层调用（框架边界层处理重试）
        executor = RetryExecutor()
        executor.execute(
            func=lambda: self.start_ffmpeg(url),
            policy_name='stream'
        )
    """

    # 预定义策略（硬编码，零配置）
    POLICIES: Dict[str, RetryPolicy] = {
        # 流操作：固定延迟 3 秒，最多 5 次
        'stream': RetryPolicy(
            max_attempts=5,
            delay=3.0,
            backoff=False
        ),

        # 数据库操作：指数退避，最多 3 次
        'database': RetryPolicy(
            max_attempts=3,
            delay=1.0,
            backoff=True,
            backoff_factor=2.0,
            max_delay=60.0
        ),

        # 外部 API：指数退避，最多 3 次
        'external_api': RetryPolicy(
            max_attempts=3,
            delay=2.0,
            backoff=True,
            backoff_factor=2.0,
            max_delay=60.0
        ),

        # 模型推理：固定延迟 1 秒，最多 2 次
        'inference': RetryPolicy(
            max_attempts=2,
            delay=1.0,
            backoff=False
        ),

        # 持久化操作：指数退避，最多 3 次
        'persistence': RetryPolicy(
            max_attempts=3,
            delay=1.0,
            backoff=True,
            backoff_factor=2.0,
            max_delay=30.0
        ),
    }

    def __init__(self, custom_policies: Optional[Dict[str, RetryPolicy]] = None):
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
        policy_name: str = 'database',
        on_retry: Optional[Callable[[int, Exception], None]] = None
    ) -> Any:
        """
        执行带重试的操作（框架边界层）

        Args:
            func: 要执行的函数（无参数，使用 lambda 或闭包传递参数）
            policy_name: 策略名称（'stream', 'database', 'external_api', etc.）
            on_retry: 重试回调函数 (attempt, exception) -> None

        Returns:
            函数执行结果

        Raises:
            最后一次尝试的异常

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

        current_delay = policy.delay

        for attempt in range(1, policy.max_attempts + 1):
            try:
                # 执行业务逻辑（可能抛出异常）
                return func()

            except Exception as e:
                # 检查是否可重试
                retryable = is_retryable_error(e)

                # 最后一次尝试，直接抛出
                if attempt == policy.max_attempts:
                    logger.error(
                        f"[RetryExecutor] Failed after {policy.max_attempts} attempts "
                        f"(policy={policy_name}): {e}",
                        exc_info=True
                    )
                    raise

                # 不可重试的异常，直接抛出
                if not retryable:
                    logger.error(
                        f"[RetryExecutor] Non-retryable exception "
                        f"(policy={policy_name}): {e}",
                        exc_info=True
                    )
                    raise

                # 计算延迟
                if policy.backoff:
                    actual_delay = min(
                        policy.delay * (policy.backoff_factor ** (attempt - 1)),
                        policy.max_delay
                    )
                else:
                    actual_delay = policy.delay

                # 记录重试日志
                logger.warning(
                    f"[RetryExecutor] Retry {attempt}/{policy.max_attempts} "
                    f"after {actual_delay:.2f}s (policy={policy_name}, error={str(e)[:100]})"
                )

                # 调用回调
                if on_retry:
                    on_retry(attempt, e)

                # 等待后重试
                time.sleep(actual_delay)


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

    def __init__(
        self,
        name: str,
        max_failures: int = 5,
        reset_timeout: float = 60.0
    ):
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
                logger.info(f"[CircuitBreaker] {self.name} success, reset failure count")
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

    组合 RetryExecutor 和 CircuitBreaker，适用于数据库和外部 API

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
        policy_name: str = 'database',
        breaker_name: str = 'default',
        max_failures: int = 5,
        reset_timeout: float = 60.0
    ):
        """
        初始化执行器

        Args:
            policy_name: 重试策略名称
            breaker_name: 熔断器名称
            max_failures: 熔断器失败阈值
            reset_timeout: 熔断器重置超时
        """
        self.retry_executor = RetryExecutor()
        self.circuit_breaker = CircuitBreaker(
            name=breaker_name,
            max_failures=max_failures,
            reset_timeout=reset_timeout
        )
        self.policy_name = policy_name

    def execute(
        self,
        func: Callable[[], Any],
        on_retry: Optional[Callable[[int, Exception], None]] = None
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
                func=func,
                policy_name=self.policy_name,
                on_retry=on_retry
            )
        )
