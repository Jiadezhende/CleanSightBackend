"""
告警上报重试（`app/services/alarm/alarm_worker.py`）测试

基于《实时 AI 视觉检测项目异常处理规范》验证：
1. Metrics 正确记录（retry）
2. 重试决策正确（fatal / retryable / 次数上限）
3. 重试次数与退避延迟序列
"""

import types

import pytest

from app.services.alarm import alarm_worker as alarm_worker_mod
from app.services.alarm.alarm_worker import (
    _report_with_retry,
    _retry_delay,
    _should_retry,
)
from app.types.exceptions import (
    FFmpegError,
    PersistenceError,
    StreamConnectionError,
)
from app.services.utils.metrics import retry_total


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """免掉告警重试的真实退避等待；返回每次 sleep 的秒数，供断言退避序列。

    只换 alarm_worker 模块里的 `time` 引用，不动全局 time.sleep。
    """
    calls = []
    monkeypatch.setattr(
        alarm_worker_mod, "time", types.SimpleNamespace(sleep=calls.append)
    )
    return calls

# ============================================================================
# 重试决策
# ============================================================================


def test_should_retry_fatal():
    """fatal=True 不重试"""
    exc = FFmpegError("FFmpeg crashed", exit_code=1)  # fatal=True
    assert _should_retry(exc, attempts=1) is False


def test_should_retry_retryable():
    """retryable=True 且未达上限则重试"""
    exc = StreamConnectionError(url="rtsp://test")  # retryable=True
    assert _should_retry(exc, attempts=1) is True


def test_should_retry_exhausted():
    """retryable=True 但已达上限（3 次）不再重试"""
    exc = StreamConnectionError(url="rtsp://test")  # retryable=True
    assert _should_retry(exc, attempts=3) is False


# ============================================================================
# Metrics 验证
# ============================================================================


def test_metrics_retry(sleeps):
    """测试 retry_total metric（operation 标签沿用 'persistence'）"""
    metric_key = ("persistence", "PersistenceError")  # (operation, 异常类名)

    # 记录前的值
    before_count = 0
    if metric_key in retry_total._metrics:
        before_count = retry_total._metrics[metric_key]._value.get()

    attempt_count = 0

    def failing_func():
        nonlocal attempt_count
        attempt_count += 1
        if attempt_count < 3:
            raise PersistenceError("Disk full", operation="hls_write")
        return "success"

    result = _report_with_retry(failing_func)

    # 验证：成功返回，retry_total 每次失败 +1，退避按指数 1s → 2s
    assert result == "success", "Should succeed after retries"
    after_count = retry_total._metrics[metric_key]._value.get()
    assert (
        after_count == before_count + 2
    ), f"retry_total should increase by 2 (before={before_count}, after={after_count})"
    assert sleeps == [1.0, 2.0]


# ============================================================================
# 重试执行
# ============================================================================


def test_report_with_retry_success():
    """首次成功直接返回结果"""
    def success_func():
        return {"result": "success"}

    assert _report_with_retry(success_func) == {"result": "success"}


def test_report_with_retry_non_retryable_error(sleeps):
    """不可重试的异常首次即上抛"""
    def non_retryable_func():
        raise FFmpegError("FFmpeg not found", exit_code=1)  # fatal=True

    with pytest.raises(FFmpegError):
        _report_with_retry(non_retryable_func)

    assert sleeps == []  # 首次失败即上抛，不退避


def test_report_with_retry_unknown_exception(sleeps):
    """非 AppError 异常不重试，原样上抛，并计入 retry_total"""
    metric_key = ("persistence", "RuntimeError")
    before_count = 0
    if metric_key in retry_total._metrics:
        before_count = retry_total._metrics[metric_key]._value.get()

    def boom():
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError):
        _report_with_retry(boom)

    assert sleeps == []
    assert retry_total._metrics[metric_key]._value.get() == before_count + 1


def test_report_with_retry_max_attempts(sleeps):
    """达到最大尝试次数后上抛"""
    attempt_count = 0

    def always_fail():
        nonlocal attempt_count
        attempt_count += 1
        raise PersistenceError("Always fail", operation="hls_write")

    with pytest.raises(PersistenceError):
        _report_with_retry(always_fail)  # 最多 3 次

    # 验证：尝试了 3 次，其间按指数退避等了 2 次
    assert (
        attempt_count == 3
    ), f"Should attempt max_attempts times (actual: {attempt_count})"
    assert sleeps == [1.0, 2.0]


def test_reporter_http_failure_is_retried(sleeps, monkeypatch):
    """真实 AlarmReporter 的 HTTP 失败抛可重试 PersistenceError，经 _report_with_retry 重试到上限"""
    from app.services.alarm.reporter import AlarmReporter

    reporter = AlarmReporter()
    sent = []
    monkeypatch.setattr(reporter, "_send_alarm_http", lambda info: sent.append(info) or False)

    with pytest.raises(PersistenceError) as exc_info:
        _report_with_retry(lambda: reporter.report_alarm({"task_id": 7, "step_id": 2}))

    assert exc_info.value.retryable
    assert len(sent) == 3
    assert sleeps == [1.0, 2.0]


# ============================================================================
# 延迟计算
# ============================================================================


def test_retry_delay_exponential_backoff():
    """指数退避：1.0, 2.0, 4.0, 8.0"""
    assert _retry_delay(1) == 1.0
    assert _retry_delay(2) == 2.0
    assert _retry_delay(3) == 4.0
    assert _retry_delay(4) == 8.0


def test_retry_delay_capped_at_max():
    """指数退避超过 30s 上限后被截断"""
    assert _retry_delay(6) == 30.0, "2**5 = 32 应被截到 30"
    assert _retry_delay(10) == 30.0


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
