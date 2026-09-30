"""
测试边界层异常处理

4 个边界层：
1. guarded_run() - worker 线程自愈（app/services/utils/worker_guard.py，边界层 1）
2. 告警上报重试 - 函数级重试（app/services/alarm/alarm_worker.py，边界层 2）
3. FastAPI 全局处理器 - HTTP 边界层（边界层 3）
4. main() - 顶层 Fail-Fast（边界层 4）

本文件覆盖边界层 3 的全局异常处理器；
告警上报的重试/退避见 test_exception_handling.py。
"""

import uuid

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.types.exceptions import (
    DatabaseError,
    FFmpegError,
    ModelInferenceError,
    PersistenceError,
    StreamConnectionError,
)


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
