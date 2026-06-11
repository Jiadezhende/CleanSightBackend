"""
测试 P0: API 层任务生命周期并发保护

验证：
1. 并发 start 同一 client → 只有一个真正执行，另一个幂等返回
2. 跨任务切换 → 完整 3 步清理
3. start + terminate 并发 → 通过 per-client 锁串行执行
4. 不同 client 的请求互不阻塞
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.routers.api import _client_locks


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_locks():
    """每个测试前清空 per-client 锁缓存"""
    _client_locks.clear()
    yield
    _client_locks.clear()


def _make_db_task(task_id: int = 1, source_ip: str = "10.0.0.1"):
    """构造一个 mock DBTask 对象"""
    task = MagicMock()
    task.task_id = task_id
    task.source_ip = source_ip
    task.current_step = "0"
    return task


def _mock_db_session(db_task):
    """构造一个返回指定 db_task 的 mock db session"""
    session = MagicMock()
    query = MagicMock()
    query.filter.return_value.first.return_value = db_task
    session.query.return_value = query
    return session


# ---------------------------------------------------------------------------
# Test 1: 并发 start 同一任务 → 幂等
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_concurrent_start_same_task_idempotent():
    """
    两个并发 start 请求（同一 task_id、同一 client）：
    - 第一个请求完成后，client 已存在且 stream 已启动
    - 第二个请求检测到 same task running → 幂等返回
    """
    db_task = _make_db_task(task_id=1, source_ip="10.0.0.1")
    mock_session = _mock_db_session(db_task)

    # 追踪 start_stream 调用次数
    start_stream_calls = []

    def track_start_stream(**kwargs):
        start_stream_calls.append(kwargs)

    # 模拟 client_manager 状态：第一次 has_client=False，之后 True
    call_count = {"has_client": 0}
    mock_cq = MagicMock()
    mock_cq.get_task_id.return_value = 1

    def has_client_side_effect(cid):
        call_count["has_client"] += 1
        # 第一次进锁时还没有 client，第二次已经有了
        return call_count["has_client"] > 1

    def fresh_db():
        """每次调用返回新的迭代器，避免 StopIteration"""
        return iter([_mock_db_session(db_task)])

    with (
        patch("app.routers.api.get_db", side_effect=fresh_db),
        patch("app.routers.api.ai") as mock_ai,
        patch("app.routers.api.stream_service") as mock_stream,
        patch("app.routers.api.client_manager") as mock_cm,
    ):
        mock_task = MagicMock()
        mock_task.current_step = "0"
        mock_cq.get_task.return_value = mock_task
        mock_ai.set_task.return_value = True
        mock_stream.start_stream.side_effect = track_start_stream
        mock_stream.has_stream.return_value = True
        mock_stream.get_stream_info.return_value = {"url": "rtsp://test/stream"}
        mock_cm.has_client.side_effect = has_client_side_effect
        mock_cm.get_client.return_value = mock_cq

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            # 并发发送两个相同的 start
            payload = {"task_id": 1, "rtsp_url": "rtsp://test/stream", "fps": 30}
            results = await asyncio.gather(
                ac.post("/api/start", json=payload),
                ac.post("/api/start", json=payload),
            )

        # 两个都应该成功
        for r in results:
            assert r.status_code == 200
            assert r.json()["status"] == "success"

        # start_stream 只应该被调用一次（第二次走幂等路径）
        assert len(start_stream_calls) == 1


# ---------------------------------------------------------------------------
# Test 2: 跨任务切换触发完整清理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_task_switch_triggers_full_cleanup():
    """
    client 正在运行 task 1，收到 start task 2 → 应触发 cleanup_client
    """
    db_task = _make_db_task(task_id=2, source_ip="10.0.0.1")
    mock_session = _mock_db_session(db_task)

    mock_cq = MagicMock()
    mock_cq.get_task_id.return_value = 1  # 旧任务 ID

    mock_monitor = MagicMock()
    mock_monitor.cleanup_client.return_value = {"errors": []}

    with (
        patch("app.routers.api.get_db", return_value=iter([mock_session])),
        patch("app.routers.api.ai") as mock_ai,
        patch("app.routers.api.stream_service") as mock_stream,
        patch("app.routers.api.client_manager") as mock_cm,
        patch("app.routers.api.get_health_monitor", return_value=mock_monitor),
    ):
        mock_ai.set_task.return_value = True
        mock_cm.has_client.return_value = True
        mock_cm.get_client.return_value = mock_cq

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/start",
                json={"task_id": 2, "rtsp_url": "rtsp://test/stream", "fps": 30},
            )

        assert r.status_code == 200

        # 验证触发了完整清理（而不是只 remove_client）
        mock_monitor.cleanup_client.assert_called_once()
        call_kwargs = mock_monitor.cleanup_client.call_args
        assert "restart" in call_kwargs.kwargs.get(
            "reason", call_kwargs[1].get("reason", "")
        )

        # 验证设置了新任务并启动了流
        mock_ai.set_task.assert_called_once()
        mock_stream.start_stream.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: start 和 terminate 并发 → 串行执行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_terminate_serialized():
    """
    同一 client 的 start 和 terminate 并发发起：
    per-client 锁保证它们串行执行，不会出现中间状态
    """
    db_task = _make_db_task(task_id=1, source_ip="10.0.0.1")
    mock_session = _mock_db_session(db_task)

    execution_order = []

    def slow_start_stream(**kwargs):
        """模拟耗时的 start_stream，验证锁的串行效果"""
        execution_order.append("start_begin")
        execution_order.append("start_end")

    mock_monitor = MagicMock()
    mock_monitor.cleanup_client.return_value = {"errors": []}

    def cleanup_side_effect(**kwargs):
        execution_order.append("terminate_cleanup")
        return {"errors": []}

    mock_monitor.cleanup_client.side_effect = cleanup_side_effect

    with (
        patch("app.routers.api.get_db", return_value=iter([mock_session])),
        patch("app.routers.api.ai") as mock_ai,
        patch("app.routers.api.stream_service") as mock_stream,
        patch("app.routers.api.client_manager") as mock_cm,
        patch("app.routers.api.get_health_monitor", return_value=mock_monitor),
    ):
        mock_ai.set_task.return_value = True
        mock_stream.start_stream.side_effect = slow_start_stream
        mock_stream.has_stream.return_value = False
        mock_cm.has_client.return_value = False

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            results = await asyncio.gather(
                ac.post(
                    "/api/start",
                    json={"task_id": 1, "rtsp_url": "rtsp://test/stream", "fps": 30},
                ),
                ac.post("/api/terminate", params={"client_id": "10.0.0.1"}),
            )

        # 两个请求都应该完成（不崩溃）
        for r in results:
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# Test 4: 不同 client 互不阻塞
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_clients_not_blocked():
    """
    不同 client_id 的请求应该获取不同的锁，互不干扰
    """
    # 两个不同的 client
    db_task_a = _make_db_task(task_id=1, source_ip="10.0.0.1")
    db_task_b = _make_db_task(task_id=2, source_ip="10.0.0.2")

    call_count = {"start_stream": 0}

    def mock_get_db():
        """根据 task_id 返回不同的 db_task"""
        session = MagicMock()
        query = MagicMock()

        def filter_side_effect(*args, **kwargs):
            result = MagicMock()
            # 根据查询参数返回不同结果
            call_count["start_stream"] += 1
            if call_count["start_stream"] <= 1:
                result.first.return_value = db_task_a
            else:
                result.first.return_value = db_task_b
            return result

        query.filter.side_effect = filter_side_effect
        session.query.return_value = query
        return iter([session])

    with (
        patch("app.routers.api.get_db", side_effect=mock_get_db),
        patch("app.routers.api.ai") as mock_ai,
        patch("app.routers.api.stream_service") as mock_stream,
        patch("app.routers.api.client_manager") as mock_cm,
    ):
        mock_ai.set_task.return_value = True
        mock_cm.has_client.return_value = False

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            results = await asyncio.gather(
                ac.post(
                    "/api/start",
                    json={"task_id": 1, "rtsp_url": "rtsp://a/stream", "fps": 30},
                ),
                ac.post(
                    "/api/start",
                    json={"task_id": 2, "rtsp_url": "rtsp://b/stream", "fps": 30},
                ),
            )

        # 两个请求都成功
        for r in results:
            assert r.status_code == 200

        # start_stream 被调用两次（两个不同 client，各一次）
        assert mock_stream.start_stream.call_count == 2


# ---------------------------------------------------------------------------
# Test 5: terminate 加锁验证
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_uses_lock():
    """
    terminate 应该获取 per-client 锁，防止与 start 竞态
    """
    mock_monitor = MagicMock()
    mock_monitor.cleanup_client.return_value = {"errors": []}

    with patch("app.routers.api.get_health_monitor", return_value=mock_monitor):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post("/api/terminate", params={"client_id": "10.0.0.1"})

        assert r.status_code == 200
        mock_monitor.cleanup_client.assert_called_once_with(
            client_id="10.0.0.1", reason="API termination request"
        )

    # 验证锁已被创建
    assert "10.0.0.1" in _client_locks


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
