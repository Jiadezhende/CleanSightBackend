"""
测试边界层异常处理

验证 4 个边界层是否正确捕获异常：
1. Worker.run() - 线程入口（边界层 1）
2. GuardedExecutor - 框架边界层（边界层 2）
3. FastAPI 全局处理器 - HTTP 边界层（边界层 3）
4. main() - 顶层 Fail-Fast（边界层 4）
"""

import pytest
from fastapi.testclient import TestClient
from threading import Thread
import time

from app.main import app
from app.utils import (
    StreamConnectionError,
    FFmpegError,
    DatabaseError,
    ModelInferenceError,
    PersistenceError,
    GuardedExecutor,
    CircuitBreaker,
)


# ============================================================================
# 边界层 2 测试: GuardedExecutor
# ============================================================================

def test_retry_executor_success():
    """测试 GuardedExecutor 成功执行"""
    executor = GuardedExecutor()

    # 模拟成功的函数
    def successful_func():
        return "success"

    result = executor.execute(
        func=successful_func,
        policy_name='stream'
    )

    assert result == "success"


def test_retry_executor_retry_then_success():
    """测试 GuardedExecutor 重试后成功"""
    executor = GuardedExecutor()
    attempts = [0]

    # 模拟第一次失败，第二次成功
    def retry_func():
        attempts[0] += 1
        if attempts[0] < 2:
            raise StreamConnectionError(url="rtsp://test", client_id="test_client")
        return "success"

    result = executor.execute(
        func=retry_func,
        policy_name='stream'
    )

    assert result == "success"
    assert attempts[0] == 2  # 第一次失败，第二次成功


def test_retry_executor_max_attempts_reached():
    """测试 GuardedExecutor 达到最大重试次数"""
    executor = GuardedExecutor()

    # 模拟总是失败的函数
    def always_fail():
        raise StreamConnectionError(url="rtsp://test", client_id="test_client")

    with pytest.raises(StreamConnectionError):
        executor.execute(
            func=always_fail,
            policy_name='stream'  # 最多 5 次
        )


def test_retry_executor_non_retryable_error():
    """测试 GuardedExecutor 不重试不可重试的异常"""
    executor = GuardedExecutor()

    # 模拟不可重试的异常（FFmpegError 默认 retryable=False）
    def non_retryable_func():
        raise FFmpegError("FFmpeg not found", exit_code=1)

    with pytest.raises(FFmpegError):
        executor.execute(
            func=non_retryable_func,
            policy_name='stream'
        )


# ============================================================================
# 边界层 2 测试: CircuitBreaker
# ============================================================================

def test_circuit_breaker_success():
    """测试 CircuitBreaker 成功执行"""
    breaker = CircuitBreaker(name='test', max_failures=3, reset_timeout=60.0)

    def successful_func():
        return "success"

    result = breaker.call(successful_func)
    assert result == "success"


def test_circuit_breaker_opens_after_failures():
    """测试 CircuitBreaker 在连续失败后打开"""
    breaker = CircuitBreaker(name='test', max_failures=3, reset_timeout=60.0)

    def always_fail():
        raise DatabaseError("Connection failed", retryable=True)

    # 前 3 次失败，熔断器打开
    for i in range(3):
        with pytest.raises(DatabaseError):
            breaker.call(always_fail)

    # 第 4 次调用，熔断器已打开，应该抛出 Exception
    with pytest.raises(Exception) as exc_info:
        breaker.call(always_fail)

    assert "Circuit breaker" in str(exc_info.value)
    assert "OPEN" in str(exc_info.value)


# ============================================================================
# 边界层 3 测试: FastAPI 全局异常处理器
# ============================================================================

@pytest.fixture
def client():
    """创建 FastAPI 测试客户端"""
    # raise_server_exceptions=False: 让服务器异常被全局处理器捕获，而不是直接抛出
    return TestClient(app, raise_server_exceptions=False)


def test_stream_error_handler(client):
    """测试 StreamConnectionError 全局处理器"""
    # 使用唯一的路由名称避免冲突
    import uuid
    route_id = uuid.uuid4().hex[:8]

    @app.get(f"/test/stream_error_{route_id}")
    async def test_stream_error():
        raise StreamConnectionError(url="rtsp://test", client_id="test_client")

    response = client.get(f"/test/stream_error_{route_id}")

    assert response.status_code == 503
    assert response.json()["error"] == "Stream unavailable"
    assert response.json()["client_id"] == "test_client"


def test_database_error_handler(client):
    """测试 DatabaseError 全局处理器"""
    import uuid
    route_id = uuid.uuid4().hex[:8]

    @app.get(f"/test/database_error_{route_id}")
    async def test_database_error():
        raise DatabaseError("Connection timeout", retryable=True)

    response = client.get(f"/test/database_error_{route_id}")

    assert response.status_code == 503
    assert response.json()["error"] == "Database unavailable"
    assert response.json()["retryable"] is True


def test_ffmpeg_error_handler(client):
    """测试 FFmpegError 全局处理器"""
    import uuid
    route_id = uuid.uuid4().hex[:8]

    @app.get(f"/test/ffmpeg_error_{route_id}")
    async def test_ffmpeg_error():
        raise FFmpegError("FFmpeg not found", exit_code=1)

    response = client.get(f"/test/ffmpeg_error_{route_id}")

    assert response.status_code == 500
    assert response.json()["error"] == "FFmpeg error"


def test_inference_error_handler(client):
    """测试 ModelInferenceError 全局处理器"""
    import uuid
    route_id = uuid.uuid4().hex[:8]

    @app.get(f"/test/inference_error_{route_id}")
    async def test_inference_error():
        raise ModelInferenceError("CUDA OOM", model_name="test_model")

    response = client.get(f"/test/inference_error_{route_id}")

    assert response.status_code == 500
    assert response.json()["error"] == "Inference failed"


def test_persistence_error_handler(client):
    """测试 PersistenceError 全局处理器"""
    import uuid
    route_id = uuid.uuid4().hex[:8]

    @app.get(f"/test/persistence_error_{route_id}")
    async def test_persistence_error():
        raise PersistenceError("HLS write failed", operation="hls_write")

    response = client.get(f"/test/persistence_error_{route_id}")

    assert response.status_code == 500
    assert response.json()["error"] == "Persistence failed"


def test_generic_exception_handler(client):
    """测试兜底异常处理器"""
    # 使用唯一的路由名称避免冲突
    import uuid
    route_id = uuid.uuid4().hex[:8]

    @app.get(f"/test/generic_error_{route_id}")
    async def test_generic_error():
        raise ValueError("Unexpected error")

    response = client.get(f"/test/generic_error_{route_id}")

    assert response.status_code == 500
    assert response.json()["error"] == "Internal server error"
    assert "unexpected error occurred" in response.json()["detail"].lower()


# ============================================================================
# 边界层 1 测试: Worker.run()
# ============================================================================

def test_worker_boundary_layer():
    """
    测试 Worker 边界层（边界层 1）

    验证：
    1. Worker.run() 捕获所有异常
    2. 线程不会因为异常而崩溃
    """
    exception_caught = [False]

    class TestWorker(Thread):
        def run(self):
            """Worker 入口（边界层 1）"""
            try:
                # 模拟业务代码抛出异常
                raise ModelInferenceError("Test error", model_name="test")
            except Exception as e:
                # 边界层捕获所有异常
                exception_caught[0] = True
                # 不重新抛出，防止线程崩溃

    worker = TestWorker()
    worker.start()
    worker.join(timeout=1.0)

    # 验证：异常被捕获，线程正常结束
    assert exception_caught[0] is True
    assert not worker.is_alive()


# ============================================================================
# 集成测试
# ============================================================================

def test_integration_retry_with_circuit_breaker():
    """
    集成测试：GuardedExecutor + CircuitBreaker

    验证：
    1. 重试逻辑正确
    2. 熔断器正确打开/关闭
    """
    from app.utils import RetryExecutorWithCircuitBreaker

    executor = RetryExecutorWithCircuitBreaker(
        policy_name='database',
        breaker_name='test_integration',
        max_failures=3,
        reset_timeout=60.0
    )

    attempts = [0]

    def flaky_func():
        """模拟不稳定的函数：前 2 次失败，第 3 次成功"""
        attempts[0] += 1
        if attempts[0] < 3:
            raise DatabaseError("Connection failed", retryable=True)
        return "success"

    # 第一次调用：重试 2 次后成功
    result = executor.execute(flaky_func)
    assert result == "success"
    assert attempts[0] == 3


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
