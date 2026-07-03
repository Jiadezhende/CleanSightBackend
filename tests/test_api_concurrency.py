"""
测试 P0: 任务生命周期并发保护（编排在 RunController，锁在 ClientManager.lock_for）

验证：
1. 并发 start 同一 client → 只有一个真正执行，另一个幂等返回
2. 跨任务切换 → 触发重启清理（stop_run）后再建新任务
3. start + terminate 并发 → 经 per-client 锁串行执行，不崩溃/死锁
4. 不同 client 的请求互不阻塞
5. terminate 获取 per-client 锁（client_manager.lock_for）

说明：编排逻辑已从 api.py 收敛到 RunController，故 mock 打在
`app.services.run_control.*`；`client_manager` 的 has_client/get/remove 用 patch.object
就地替换，但**保留真实 lock_for**（真锁 → 真串行），故断言真实 `_task_locks`。
"""

import asyncio
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

from app.main import app
from app.services.client.manager import client_manager


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_locks():
    """每个测试前后清空 per-client 任务级锁缓存"""
    client_manager._task_locks.clear()
    yield
    client_manager._task_locks.clear()


def _make_db_task(task_id: int = 1, source_ip: str = "10.0.0.1"):
    task = MagicMock()
    task.task_id = task_id
    task.source_ip = source_ip
    task.current_step = "0"
    return task


def _mock_db_session(db_task):
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
    """两个并发 start（同 task_id/同 client）：第一个建，第二个幂等返回。"""
    db_task = _make_db_task(task_id=1, source_ip="10.0.0.1")

    start_stream_calls = []

    def track_start_stream(**kwargs):
        start_stream_calls.append(kwargs)

    call_count = {"has_client": 0}
    mock_cq = MagicMock()
    mock_cq.get_task_id.return_value = 1
    mock_cq.current_step = "0"  # 幂等比对读 old_cq.current_step

    def has_client_side_effect(cid):
        call_count["has_client"] += 1
        return call_count["has_client"] > 1  # 首次未建、之后已建

    def fresh_db():
        return iter([_mock_db_session(db_task)])

    with (
        patch("app.routers.api.get_db", side_effect=fresh_db),
        patch("app.services.run_control.inference_manager") as mock_inference,
        patch("app.services.run_control.stream_service") as mock_stream,
        patch("app.services.run_control.persistence_manager"),
        patch("app.services.run_control.ClientQueues"),
        patch.object(client_manager, "has_client", side_effect=has_client_side_effect),
        patch.object(client_manager, "get", return_value=mock_cq),
    ):
        mock_inference.start_workflow.return_value = True
        mock_inference.resolve_stage.return_value = "0"
        mock_stream.start_stream.side_effect = track_start_stream
        mock_stream.get_stream_info.return_value = {"url": "rtsp://test/stream"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"task_id": 1, "rtsp_url": "rtsp://test/stream", "fps": 30}
            results = await asyncio.gather(
                ac.post("/api/start", json=payload),
                ac.post("/api/start", json=payload),
            )

        for r in results:
            assert r.status_code == 200
            assert r.json()["status"] == "success"

        # 幂等：start_stream 只被调一次
        assert len(start_stream_calls) == 1


# ---------------------------------------------------------------------------
# Test 2: 跨任务切换 → 触发重启清理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_task_url_change_triggers_restart():
    """同 task_id（同 run_key 槽位）改 URL → 先 stop_run 拆旧、再建新（重启语义）。

    换键后运行键 = str(task_id)：抢占/重启只在**同 task_id**改 step/url 时发生；
    不同 task_id 走不同槽位、天然并发（见 test_different_clients_not_blocked）。
    """
    db_task = _make_db_task(task_id=1, source_ip="10.0.0.1")

    mock_cq = MagicMock()
    mock_cq.get_task_id.return_value = 1
    mock_cq.current_step = "0"  # step 同，但下方 URL 不同 → 非幂等，触发重启

    with (
        patch("app.routers.api.get_db", return_value=iter([_mock_db_session(db_task)])),
        patch("app.services.run_control.inference_manager") as mock_inference,
        patch("app.services.run_control.stream_service") as mock_stream,
        patch("app.services.run_control.persistence_manager"),
        patch("app.services.run_control.ClientQueues"),
        patch.object(client_manager, "has_client", return_value=True),
        patch.object(client_manager, "get", return_value=mock_cq),
        patch.object(
            client_manager, "remove", return_value={"removed": True, "error": None}
        ),
    ):
        mock_inference.start_workflow.return_value = True
        mock_inference.resolve_stage.return_value = "0"
        # 旧流 URL 与新请求不同 → 非幂等
        mock_stream.get_stream_info.return_value = {"url": "rtsp://old/stream"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post(
                "/api/start",
                json={"task_id": 1, "rtsp_url": "rtsp://new/stream", "fps": 30},
            )

        assert r.status_code == 200

        # 重启清理（stop_run）：停旧流 + 落盘旧数据
        mock_stream.stop_stream.assert_called_once()
        mock_inference.stop_workflow.assert_called_once()
        # 建新任务 + 起新流
        mock_inference.start_workflow.assert_called_once()
        mock_stream.start_stream.assert_called_once()


# ---------------------------------------------------------------------------
# Test 3: start 和 terminate 并发 → 串行、不崩溃
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_terminate_serialized():
    """同一 run（task_id=1）的 start 与 terminate 并发：经 lock_for 串行，均正常完成。"""
    db_task = _make_db_task(task_id=1, source_ip="10.0.0.1")

    mock_cq = MagicMock()
    mock_cq.run_key = "1"

    with (
        patch("app.routers.api.get_db", return_value=iter([_mock_db_session(db_task)])),
        patch("app.services.run_control.inference_manager") as mock_inference,
        patch("app.services.run_control.stream_service") as mock_stream,
        patch("app.services.run_control.persistence_manager"),
        patch("app.services.run_control.ClientQueues"),
        patch.object(client_manager, "has_client", return_value=False),
        patch.object(client_manager, "get", return_value=mock_cq),
        patch.object(client_manager, "find_by_source_ip", return_value=mock_cq),
        patch.object(
            client_manager, "remove", return_value={"removed": True, "error": None}
        ),
    ):
        mock_inference.start_workflow.return_value = True
        mock_inference.resolve_stage.return_value = "0"

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            results = await asyncio.gather(
                ac.post(
                    "/api/start",
                    json={"task_id": 1, "rtsp_url": "rtsp://test/stream", "fps": 30},
                ),
                ac.post("/api/terminate", params={"client_id": "10.0.0.1"}),
            )

        for r in results:
            assert r.status_code == 200


# ---------------------------------------------------------------------------
# Test 4: 不同 client 互不阻塞
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_clients_not_blocked():
    """不同 client_id → 不同 lock_for，互不干扰，各起一次流。"""
    db_task_a = _make_db_task(task_id=1, source_ip="10.0.0.1")
    db_task_b = _make_db_task(task_id=2, source_ip="10.0.0.2")

    call_count = {"q": 0}

    def mock_get_db():
        session = MagicMock()
        query = MagicMock()

        def filter_side_effect(*args, **kwargs):
            result = MagicMock()
            call_count["q"] += 1
            result.first.return_value = db_task_a if call_count["q"] <= 1 else db_task_b
            return result

        query.filter.side_effect = filter_side_effect
        session.query.return_value = query
        return iter([session])

    with (
        patch("app.routers.api.get_db", side_effect=mock_get_db),
        patch("app.services.run_control.inference_manager") as mock_inference,
        patch("app.services.run_control.stream_service") as mock_stream,
        patch("app.services.run_control.persistence_manager"),
        patch("app.services.run_control.ClientQueues"),
        patch.object(client_manager, "has_client", return_value=False),
    ):
        mock_inference.start_workflow.return_value = True
        mock_inference.resolve_stage.return_value = "0"

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            results = await asyncio.gather(
                ac.post("/api/start", json={"task_id": 1, "rtsp_url": "rtsp://a/stream", "fps": 30}),
                ac.post("/api/start", json={"task_id": 2, "rtsp_url": "rtsp://b/stream", "fps": 30}),
            )

        for r in results:
            assert r.status_code == 200
        assert mock_stream.start_stream.call_count == 2


# ---------------------------------------------------------------------------
# Test 5: terminate 获取 per-client 锁
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_terminate_uses_lock():
    """terminate（wire=source_ip）经垫片解析 run → stop_run 持 lock_for(run_key)、stop_workflow(cq)。"""
    mock_cq = MagicMock()
    mock_cq.run_key = "1"

    with (
        patch("app.services.run_control.inference_manager") as mock_inference,
        patch("app.services.run_control.stream_service") as mock_stream,
        patch("app.services.run_control.persistence_manager"),
        patch.object(client_manager, "find_by_source_ip", return_value=mock_cq),
        patch.object(client_manager, "get", return_value=mock_cq),
        patch.object(client_manager, "has_client", return_value=True),
        patch.object(
            client_manager, "remove", return_value={"removed": True, "error": None}
        ),
    ):
        mock_inference.stop_workflow.return_value = []
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post("/api/terminate", params={"client_id": "10.0.0.1"})

        assert r.status_code == 200
        mock_inference.stop_workflow.assert_called_once_with(mock_cq)

    # 验证真实的 per-run 锁已按 run_key 创建
    assert "1" in client_manager._task_locks


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
