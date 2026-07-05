"""
CleanSight 异常处理集成测试

基于《实时 AI 视觉检测项目异常处理规范》验证：
1. Metrics 正确记录（retry / gpu_oom）
2. Action 决策正确（RETRY/FATAL）
3. 重试/退避策略与端到端持久化重试
"""

import time
from unittest.mock import Mock, patch

import pytest

from app.utils.exceptions import (
    DatabaseError,
    FFmpegError,
    ModelInferenceError,
    PersistenceError,
    StreamConnectionError,
)
from app.utils.executor import Action, ExecutionPolicy, GuardedExecutor
from app.utils.metrics import (
    gpu_oom_total,
    retry_total,
)

# ============================================================================
# Phase 5 Test 3: Action 决策测试
# ============================================================================


def test_action_decision_fatal():
    """测试 Action.FATAL 决策（fatal=True）"""
    executor = GuardedExecutor()
    policy = ExecutionPolicy(max_attempts=3)

    exc = FFmpegError("FFmpeg crashed", exit_code=1)  # fatal=True
    action = executor._decide_action(exc, policy, attempts=1)

    assert action == Action.FATAL, "FFmpegError should result in Action.FATAL"


def test_action_decision_retry():
    """测试 Action.RETRY 决策（retryable=True）"""
    executor = GuardedExecutor()
    policy = ExecutionPolicy(max_attempts=3)

    exc = StreamConnectionError(url="rtsp://test")  # retryable=True
    action = executor._decide_action(exc, policy, attempts=1)

    assert action == Action.RETRY, "StreamConnectionError should result in Action.RETRY"


def test_action_decision_retry_exhausted():
    """测试 Action.FATAL 决策（重试耗尽）"""
    executor = GuardedExecutor()
    policy = ExecutionPolicy(max_attempts=3)

    exc = StreamConnectionError(url="rtsp://test")  # retryable=True
    action = executor._decide_action(exc, policy, attempts=3)

    # 重试次数已达上限，应该返回 FATAL
    assert action == Action.FATAL, "Retry exhausted should result in Action.FATAL"


# ============================================================================
# Phase 5 Test 4: Metrics 验证
# ============================================================================


def test_metrics_retry():
    """测试 retry_total metric"""
    executor = GuardedExecutor()

    # 修复：operation 应该是 policy_name（'stream'）
    operation = "stream"  # policy_name
    error_type = "StreamConnectionError"
    metric_key = (operation, error_type)

    # 记录前的值
    before_count = 0
    if metric_key in retry_total._metrics:
        before_count = retry_total._metrics[metric_key]._value.get()

    attempt_count = 0

    def failing_func():
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            raise StreamConnectionError(url="rtsp://test")
        return "success"

    result = executor.execute(func=failing_func, policy_name="stream")

    # 验证：成功返回，且 retry_total 增加
    assert result == "success", "Should succeed after retries"
    after_count = retry_total._metrics[metric_key]._value.get()
    # 应该重试了 2 次（第 3 次成功）
    assert (
        after_count >= before_count + 2
    ), f"retry_total should increase (before={before_count}, after={after_count})"


def test_metrics_gpu_oom():
    """测试 gpu_oom_total metric"""
    executor = GuardedExecutor()

    model_name = "test_model"
    metric_key = (model_name,)

    # 记录前的值
    before_count = 0
    if metric_key in gpu_oom_total._metrics:
        before_count = gpu_oom_total._metrics[metric_key]._value.get()

    def oom_func():
        raise ModelInferenceError(
            message="CUDA out of memory", model_name=model_name, is_cuda_error=True
        )

    with pytest.raises(ModelInferenceError):
        executor.execute(func=oom_func, policy_name="inference")

    # 验证 metric 增加
    after_count = gpu_oom_total._metrics[metric_key]._value.get()
    assert after_count == before_count + 1, "gpu_oom_total should increase"


# ============================================================================
# Phase 5 Test 5: GuardedExecutor 集成测试
# ============================================================================


def test_retry_executor_success():
    """测试 GuardedExecutor 成功执行"""
    executor = GuardedExecutor()

    def success_func():
        return {"result": "success"}

    result = executor.execute(func=success_func, policy_name="database")

    assert result == {"result": "success"}


def test_retry_executor_retryable_error():
    """测试 GuardedExecutor 重试可重试的异常"""
    executor = GuardedExecutor()

    attempt_count = 0

    def retryable_func():
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            raise DatabaseError("Connection timeout", retryable=True)
        return "success"

    result = executor.execute(func=retryable_func, policy_name="database")  # 最多 3 次

    assert result == "success"
    assert attempt_count == 3, "Should retry twice before succeeding"


def test_retry_executor_non_retryable_error():
    """测试 GuardedExecutor 不重试不可重试的异常"""
    executor = GuardedExecutor()

    def non_retryable_func():
        raise FFmpegError("FFmpeg not found", exit_code=1)  # fatal=True

    with pytest.raises(FFmpegError):
        executor.execute(func=non_retryable_func, policy_name="stream")


def test_retry_executor_max_attempts():
    """测试 GuardedExecutor 达到最大重试次数"""
    executor = GuardedExecutor()

    attempt_count = 0

    def always_fail():
        nonlocal attempt_count
        attempt_count += 1
        raise DatabaseError("Always fail", retryable=True)

    with pytest.raises(DatabaseError):
        executor.execute(func=always_fail, policy_name="database")  # 最多 3 次

    # 验证：尝试了 3 次
    assert (
        attempt_count == 3
    ), f"Should attempt max_attempts times (actual: {attempt_count})"


# ============================================================================
# Phase 5 Test 6: 延迟计算测试
# ============================================================================


def test_calculate_delay_fixed():
    """测试固定延迟策略"""
    executor = GuardedExecutor()
    policy = ExecutionPolicy(delay=2.0, backoff=False)

    delay1 = executor._calculate_delay(policy, attempts=1)
    delay2 = executor._calculate_delay(policy, attempts=2)
    delay3 = executor._calculate_delay(policy, attempts=3)

    assert delay1 == 2.0
    assert delay2 == 2.0
    assert delay3 == 2.0


def test_calculate_delay_exponential_backoff():
    """测试指数退避策略"""
    executor = GuardedExecutor()
    policy = ExecutionPolicy(
        delay=1.0, backoff=True, backoff_factor=2.0, max_delay=10.0
    )

    delay1 = executor._calculate_delay(policy, attempts=1)
    delay2 = executor._calculate_delay(policy, attempts=2)
    delay3 = executor._calculate_delay(policy, attempts=3)
    delay4 = executor._calculate_delay(policy, attempts=4)

    # 指数退避：1.0, 2.0, 4.0, 8.0
    assert delay1 == 1.0
    assert delay2 == 2.0
    assert delay3 == 4.0
    assert delay4 == 8.0


def test_calculate_delay_exponential_backoff_max():
    """测试指数退避达到上限"""
    executor = GuardedExecutor()
    policy = ExecutionPolicy(
        delay=1.0, backoff=True, backoff_factor=2.0, max_delay=5.0  # 限制最大延迟
    )

    delay5 = executor._calculate_delay(policy, attempts=5)
    delay10 = executor._calculate_delay(policy, attempts=10)

    # 指数退避会超过 max_delay，应该被限制在 5.0
    assert delay5 == 5.0, f"delay5 should be capped at max_delay (actual: {delay5})"
    assert delay10 == 5.0, f"delay10 should be capped at max_delay (actual: {delay10})"


# ============================================================================
# Phase 5 Test 7: 端到端场景测试
# ============================================================================


def test_end_to_end_persistence_retry():
    """
    端到端测试：持久化重试

    场景：
    - 写入 HLS 视频段
    - 第 1 次失败（磁盘满）
    - 第 2 次成功

    验证：
    - 最终成功
    - 重试 1 次
    """
    executor = GuardedExecutor()

    attempt_count = 0

    def persist_segment():
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count == 1:
            raise PersistenceError(
                message="Disk full",
                source_ip="persist_test",
                operation="hls_write",
                retryable=True,
            )
        return "segment_saved"

    result = executor.execute(func=persist_segment, policy_name="persistence")

    assert result == "segment_saved"
    assert attempt_count == 2, "Should retry once"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
