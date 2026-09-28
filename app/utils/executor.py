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

from app.types.exceptions import AppError, ModelInferenceError

logger = logging.getLogger(__name__)


class Action(Enum):
    """异常处理动作枚举

    用于 Executor 决策显式化：
    - RETRY: 重试当前操作
    - FATAL: 致命错误，向上传播
    """

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
        def write_segment(path: Path, data: bytes):
            if not path.parent.exists():
                raise PersistenceError("dir missing", operation="hls_write")
            # ... 写盘逻辑

        # 服务层调用（框架边界层处理重试）
        executor = GuardedExecutor()
        executor.execute(
            func=lambda: write_segment(path, data),
            policy_name='persistence'
        )
    """

    # 预定义策略（硬编码，零配置）
    POLICIES: Dict[str, ExecutionPolicy] = {
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
        policy_name: str = "persistence",
        on_retry: Optional[Callable[[int, Exception], None]] = None,
    ) -> Any:
        """
        执行带重试的操作（框架边界层）

        基于《实时 AI 视觉检测项目异常处理规范》：
        - 根据异常类型和 Policy 决策处理动作（RETRY/FATAL）
        - 强制记录 metrics（成功/失败）

        Args:
            func: 要执行的函数（无参数，使用 lambda 或闭包传递参数）
            policy_name: 策略名称（POLICIES 的键，现仅 'persistence'）
            on_retry: 重试回调函数 (attempt, exception) -> None

        Returns:
            函数执行结果

        Raises:
            AppError: 致命错误或重试耗尽
            Exception: 未知异常
        """
        policy = self.policies.get(policy_name)
        if not policy:
            raise ValueError(f"Unknown policy: {policy_name}")

        attempts = 0

        while True:
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

                if action == Action.RETRY:
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

    def _decide_action(
        self, exc: AppError, policy: ExecutionPolicy, attempts: int
    ) -> Action:
        """
        决策处理动作（核心决策逻辑）

        基于《实时 AI 视觉检测项目异常处理规范》：
        - fatal=True -> FATAL（致命错误）
        - retryable=True 且未超过次数 -> RETRY（重试）
        - 其他 -> FATAL（向上传播）

        Args:
            exc: AppError 异常实例
            policy: 重试策略
            attempts: 当前尝试次数

        Returns:
            Action: 处理动作（RETRY/FATAL）
        """
        # 1. fatal=True -> FATAL
        if exc.fatal:
            return Action.FATAL

        # 2. retryable=True 且未超过次数 -> RETRY
        if exc.retryable and attempts < policy.max_attempts:
            return Action.RETRY

        # 3. 其他 -> FATAL
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
            from .metrics import retry_total

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
        from .metrics import gpu_oom_total, retry_total

        exc_type = type(exc).__name__

        # 1. 通用重试计数（所有异常）
        if action in (Action.RETRY, Action.FATAL):
            retry_total.labels(operation=policy_name, error_type=exc_type).inc()

        # 2. GPU OOM 专用计数
        if isinstance(exc, ModelInferenceError) and exc.is_cuda_error:
            gpu_oom_total.labels(model=exc.model_name or "unknown").inc()
