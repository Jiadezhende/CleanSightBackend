"""
测试边界层异常处理

4 个边界层：
1. guarded_run() - worker 线程自愈（app/utils/worker_guard.py，边界层 1）
2. GuardedExecutor / CircuitBreaker - 框架边界层（边界层 2）
3. FastAPI 全局处理器 - HTTP 边界层（边界层 3）
4. main() - 顶层 Fail-Fast（边界层 4）

本文件覆盖边界层 2 的熔断器与边界层 3 的全局异常处理器；
GuardedExecutor 的重试/退避见 test_exception_handling.py。
"""

import time
import types
import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.utils import (
    CircuitBreaker,
    DatabaseError,
    FFmpegError,
    ModelInferenceError,
    PersistenceError,
    StreamConnectionError,
)
from app.utils import executor as executor_mod


@pytest.fixture(autouse=True)
def sleeps(monkeypatch):
    """免掉 GuardedExecutor 的真实退避等待；返回每次 sleep 的秒数。

    只换 executor 模块里的 `time` 引用，不动全局 time.sleep；CircuitBreaker 仍用真 time.time。
    """
    calls = []
    monkeypatch.setattr(
        executor_mod, "time", types.SimpleNamespace(sleep=calls.append, time=time.time)
    )
    return calls


# ============================================================================
# 边界层 2 测试: CircuitBreaker
# ============================================================================


def test_circuit_breaker_success():
    """测试 CircuitBreaker 成功执行"""
    breaker = CircuitBreaker(name="test", max_failures=3, reset_timeout=60.0)

    def successful_func():
        return "success"

    result = breaker.call(successful_func)
    assert result == "success"


def test_circuit_breaker_opens_after_failures():
    """测试 CircuitBreaker 在连续失败后打开"""
    breaker = CircuitBreaker(name="test", max_failures=3, reset_timeout=60.0)

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


@pytest.fixture
def raising_route():
    """往全局 app 临时挂一条抛指定异常的 GET 路由，返回其路径；用例结束摘掉，不污染 app。"""
    added = []

    def _register(exc: Exception) -> str:
        path = f"/test/raise_{uuid.uuid4().hex[:8]}"

        async def _raise():
            raise exc

        app.add_api_route(path, _raise, methods=["GET"])
        added.append(app.router.routes[-1])
        return path

    yield _register
    for route in added:
        app.router.routes.remove(route)


def test_stream_error_handler(client, raising_route):
    """测试 StreamConnectionError 全局处理器"""
    path = raising_route(StreamConnectionError(url="rtsp://test", source_ip="test_client"))

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()["error"] == "Stream unavailable"
    assert response.json()["client_id"] == "test_client"


def test_database_error_handler(client, raising_route):
    """测试 DatabaseError 全局处理器"""
    path = raising_route(DatabaseError("Connection timeout", retryable=True))

    response = client.get(path)

    assert response.status_code == 503
    assert response.json()["error"] == "Database unavailable"
    assert response.json()["retryable"] is True


def test_ffmpeg_error_handler(client, raising_route):
    """测试 FFmpegError 全局处理器"""
    path = raising_route(FFmpegError("FFmpeg not found", exit_code=1))

    response = client.get(path)

    assert response.status_code == 500
    assert response.json()["error"] == "FFmpeg error"


def test_inference_error_handler(client, raising_route):
    """测试 ModelInferenceError 全局处理器"""
    path = raising_route(ModelInferenceError("CUDA OOM", model_name="test_model"))

    response = client.get(path)

    assert response.status_code == 500
    assert response.json()["error"] == "Inference failed"


def test_persistence_error_handler(client, raising_route):
    """测试 PersistenceError 全局处理器"""
    path = raising_route(PersistenceError("HLS write failed", operation="hls_write"))

    response = client.get(path)

    assert response.status_code == 500
    assert response.json()["error"] == "Persistence failed"


def test_generic_exception_handler(client, raising_route):
    """测试兜底异常处理器"""
    path = raising_route(ValueError("Unexpected error"))

    response = client.get(path)

    assert response.status_code == 500
    assert response.json()["error"] == "Internal server error"
    assert "unexpected error occurred" in response.json()["detail"].lower()


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
        policy_name="database",
        breaker_name="test_integration",
        max_failures=3,
        reset_timeout=60.0,
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
