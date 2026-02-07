"""
CleanSight 异常处理集成测试

基于《实时 AI 视觉检测项目异常处理规范》验证：
1. FrameDrop 被安静丢弃（返回 None）
2. 单帧失败不影响其他 9 路流
3. Metrics 正确记录
4. Action 决策正确（DROP/RETRY/FATAL）
"""

import pytest
import time
from unittest.mock import Mock, patch

from app.utils.exceptions import (
    FrameDrop,
    StreamConnectionError,
    FFmpegError,
    DatabaseError,
    ModelInferenceError,
    PersistenceError,
)
from app.utils.executor import GuardedExecutor, Action, ExecutionPolicy
from app.utils.metrics import (
    frame_drop_total,
    retry_total,
    infer_failure_total,
    gpu_oom_total,
)


# ============================================================================
# Phase 5 Test 1: FrameDrop 测试
# ============================================================================

def test_frame_drop_is_silent():
    """
    测试 FrameDrop 被安静丢弃

    验证点：
    - FrameDrop 不抛异常
    - 返回 None
    - metrics 中有丢帧记录
    """
    executor = GuardedExecutor()

    def drop_frame():
        raise FrameDrop(
            client_id="test_client",
            frame_index=42,
            reason="decode_failed"
        )

    # 执行前获取 metric 值
    metric_key = ("test_client", "decode_failed")
    before_count = 0
    if metric_key in frame_drop_total._metrics:
        before_count = frame_drop_total._metrics[metric_key]._value.get()

    # 执行（应该返回 None，不抛异常）
    result = executor.execute(
        func=drop_frame,
        policy_name='inference'
    )

    # 验证
    assert result is None, "FrameDrop should return None"

    # 验证 metrics（丢帧计数增加）
    after_count = frame_drop_total._metrics[metric_key]._value.get()
    assert after_count == before_count + 1, f"frame_drop_total should increase (before={before_count}, after={after_count})"


def test_frame_drop_with_different_reasons():
    """测试不同原因的 FrameDrop"""
    executor = GuardedExecutor()

    reasons = ["decode_failed", "client_removed", "quality_check_failed", "timeout"]

    for reason in reasons:
        def drop_frame_with_reason():
            raise FrameDrop(
                client_id=f"client_{reason}",
                reason=reason
            )

        result = executor.execute(
            func=drop_frame_with_reason,
            policy_name='inference'
        )

        assert result is None, f"FrameDrop with reason '{reason}' should return None"


# ============================================================================
# Phase 5 Test 2: 单帧失败不影响其他路
# ============================================================================

def test_single_stream_failure_doesnt_affect_others():
    """
    测试单路流失败不影响其他 9 路

    场景：
    - 10 路并发推理
    - client_5 推理失败（FrameDrop）
    - 其他 9 路正常

    验证点：
    - client_5 返回 None
    - 其他 9 路返回正常结果
    """
    executor = GuardedExecutor()

    # 模拟 10 路客户端
    clients = [f"client_{i}" for i in range(10)]

    def infer_frame(client_id):
        """模拟推理：client_5 失败，其他成功"""
        if client_id == "client_5":
            raise FrameDrop(
                client_id=client_id,
                reason="quality_check_failed"
            )
        return {"result": "success", "client_id": client_id}

    # 并发推理（模拟）
    results = []
    for client_id in clients:
        result = executor.execute(
            func=lambda cid=client_id: infer_frame(cid),
            policy_name='inference'
        )
        results.append(result)

    # 验证：client_5 返回 None，其他返回正常
    assert results[5] is None, "client_5 should return None (FrameDrop)"
    for i, result in enumerate(results):
        if i == 5:
            continue
        assert result is not None, f"client_{i} should return result"
        assert result["client_id"] == f"client_{i}", f"client_{i} result mismatch"


# ============================================================================
# Phase 5 Test 3: Action 决策测试
# ============================================================================

def test_action_decision_drop():
    """测试 Action.DROP 决策（FrameDrop）"""
    executor = GuardedExecutor()
    policy = ExecutionPolicy(max_attempts=3)

    exc = FrameDrop(client_id="test", reason="test")
    action = executor._decide_action(exc, policy, attempts=1)

    assert action == Action.DROP, "FrameDrop should result in Action.DROP"


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

def test_metrics_frame_drop():
    """测试 frame_drop_total metric"""
    executor = GuardedExecutor()

    client_id = "metrics_test_client"
    reason = "metrics_test_reason"
    metric_key = (client_id, reason)

    # 记录前的值
    before_count = 0
    if metric_key in frame_drop_total._metrics:
        before_count = frame_drop_total._metrics[metric_key]._value.get()

    def drop_frame():
        raise FrameDrop(client_id=client_id, reason=reason)

    executor.execute(func=drop_frame, policy_name='inference')

    # 验证 metric 增加
    after_count = frame_drop_total._metrics[metric_key]._value.get()
    assert after_count == before_count + 1, "frame_drop_total should increase"


def test_metrics_retry():
    """测试 retry_total metric"""
    executor = GuardedExecutor()

    operation = "test_operation"
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

    result = executor.execute(func=failing_func, policy_name='stream')

    # 验证：成功返回，且 retry_total 增加
    assert result == "success", "Should succeed after retries"
    after_count = retry_total._metrics[metric_key]._value.get()
    # 应该重试了 2 次（第 3 次成功）
    assert after_count >= before_count + 2, f"retry_total should increase (before={before_count}, after={after_count})"


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
            message="CUDA out of memory",
            model_name=model_name,
            is_cuda_error=True
        )

    with pytest.raises(ModelInferenceError):
        executor.execute(func=oom_func, policy_name='inference')

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

    result = executor.execute(
        func=success_func,
        policy_name='database'
    )

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

    result = executor.execute(
        func=retryable_func,
        policy_name='database'  # 最多 3 次
    )

    assert result == "success"
    assert attempt_count == 3, "Should retry twice before succeeding"


def test_retry_executor_non_retryable_error():
    """测试 GuardedExecutor 不重试不可重试的异常"""
    executor = GuardedExecutor()

    def non_retryable_func():
        raise FFmpegError("FFmpeg not found", exit_code=1)  # fatal=True

    with pytest.raises(FFmpegError):
        executor.execute(
            func=non_retryable_func,
            policy_name='stream'
        )


def test_retry_executor_max_attempts():
    """测试 GuardedExecutor 达到最大重试次数"""
    executor = GuardedExecutor()

    attempt_count = 0

    def always_fail():
        nonlocal attempt_count
        attempt_count += 1
        raise DatabaseError("Always fail", retryable=True)

    with pytest.raises(DatabaseError):
        executor.execute(
            func=always_fail,
            policy_name='database'  # 最多 3 次
        )

    # 验证：尝试了 3 次
    assert attempt_count == 3, f"Should attempt max_attempts times (actual: {attempt_count})"


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
        delay=1.0,
        backoff=True,
        backoff_factor=2.0,
        max_delay=10.0
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
        delay=1.0,
        backoff=True,
        backoff_factor=2.0,
        max_delay=5.0  # 限制最大延迟
    )

    delay5 = executor._calculate_delay(policy, attempts=5)
    delay10 = executor._calculate_delay(policy, attempts=10)

    # 指数退避会超过 max_delay，应该被限制在 5.0
    assert delay5 == 5.0, f"delay5 should be capped at max_delay (actual: {delay5})"
    assert delay10 == 5.0, f"delay10 should be capped at max_delay (actual: {delay10})"


# ============================================================================
# Phase 5 Test 7: 端到端场景测试
# ============================================================================

def test_end_to_end_inference_with_frame_drop():
    """
    端到端测试：推理场景中的 FrameDrop

    场景：
    - 批量推理 5 帧
    - 第 3 帧解码失败（FrameDrop）
    - 其他 4 帧成功

    验证：
    - 第 3 帧返回 None
    - 其他帧正常返回
    - metrics 正确记录
    """
    executor = GuardedExecutor()

    frames = [f"frame_{i}" for i in range(5)]

    def infer_single_frame(frame_id):
        """模拟单帧推理"""
        if frame_id == "frame_2":
            raise FrameDrop(
                client_id="inference_test",
                frame_index=2,
                reason="decode_failed"
            )
        return {"frame": frame_id, "result": "OK"}

    results = []
    for frame in frames:
        result = executor.execute(
            func=lambda f=frame: infer_single_frame(f),
            policy_name='inference'
        )
        results.append(result)

    # 验证
    assert len(results) == 5
    assert results[0] is not None
    assert results[1] is not None
    assert results[2] is None, "frame_2 should be dropped (None)"
    assert results[3] is not None
    assert results[4] is not None


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
                client_id="persist_test",
                operation="hls_write",
                retryable=True
            )
        return "segment_saved"

    result = executor.execute(
        func=persist_segment,
        policy_name='persistence'
    )

    assert result == "segment_saved"
    assert attempt_count == 2, "Should retry once"


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
