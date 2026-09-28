"""
测试 P0: 任务生命周期并发保护（编排在 RunControlService，锁在 ClientService.lock_for）

验证：
1. 同 task 已在跑、step 与 url 都没变 → 再 start 幂等返回，不重起流
2. 同 task 改 url → 触发重启清理（stop_run）后再建新任务
3. 同 task 的 start + terminate 并发 → 经 per-task 锁串行：terminate 的拆除等 start 做完
4. 不同 task 的 start 互不阻塞：两边能同时处在各自的持锁段里
5. terminate（body `{task_id}` 首选入口 / `?client_id=` 兼容入口）获取 per-task 锁

说明：编排逻辑已从 api.py 收敛到 RunControlService，故 mock 打在
`app.services.run_control.service.*`；**保留真实 lock_for**（真锁 → 真串行），故断言真实
`_task_locks`。注册表按用例决定：要看「上一次 start 登记的 run」的用例用真实注册表
（每个用例换一本空表，不污染全局），其余用 patch.object 就地替换 has_client/get/remove。
"""

import asyncio
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport
from sqlalchemy.exc import SQLAlchemyError

from app.db import tasks as db_tasks
from app.types.run import RunIdentity
from app.main import app
from app.services.client.instance import client_service

_real_query_task = db_tasks.query_task


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_client_service(monkeypatch):
    """每个用例一本空注册表 + 清空 per-task 锁缓存；结束后原样恢复"""
    monkeypatch.setattr(client_service, "_runs", {})
    client_service._task_locks.clear()
    yield
    client_service._task_locks.clear()


def _make_db_task(task_id: int = 1, source_ip: str = "10.0.0.1"):
    task = MagicMock()
    task.task_id = task_id
    task.source_ip = source_ip
    task.current_step = "0"
    return task


def _new_cq(*, run, **_kwargs):
    """ClientQueues 替身：每次 start 一个新对象，带上真实的 run 身份"""
    cq = MagicMock()
    cq.run = run
    return cq


# ---------------------------------------------------------------------------
# Test 1: 同 task、step 与 url 不变 → 幂等
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_restart_with_same_step_and_url_is_idempotent():
    """同 task 连续两次 start（step、url 均相同）：第二次幂等返回，不拆旧、不重建、不重起流。"""
    db_task = _make_db_task(task_id=1, source_ip="10.0.0.1")

    with (
        patch.object(db_tasks, "query_task", return_value=db_task),
        patch("app.services.run_control.service.inference_service") as mock_inference,
        patch("app.services.run_control.service.stream_service") as mock_stream,
        patch("app.services.run_control.service.recording_service"),
        patch("app.services.run_control.service.ClientQueues", side_effect=_new_cq),
    ):
        mock_inference.start_workflow.return_value = True
        mock_inference.resolve_stage.return_value = "0"
        mock_stream.get_stream_info.return_value = {"url": "rtsp://test/stream"}

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            payload = {"task_id": 1, "rtsp_url": "rtsp://test/stream"}
            first = await ac.post("/api/start", json=payload)
            second = await ac.post("/api/start", json=payload)

    assert first.status_code == 200 and second.status_code == 200
    assert "idempotent" not in first.json()["message"]
    assert "idempotent" in second.json()["message"]
    # 只有第一次真正建任务、起流；第二次没有拆旧
    mock_inference.start_workflow.assert_called_once()
    mock_stream.start_stream.assert_called_once()
    mock_stream.stop_stream.assert_not_called()


# ---------------------------------------------------------------------------
# Test 2: 同 task 改 url → 触发重启清理
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_task_url_change_triggers_restart():
    """同 task_id（同 int 键槽位）改 URL → 先 stop_run 拆旧、再建新（重启语义）。

    运行键 = task_id：抢占/重启只在**同 task_id**改 step/url 时发生；
    不同 task_id 走不同槽位、天然并发（见 test_different_tasks_not_blocked）。
    """
    db_task = _make_db_task(task_id=1, source_ip="10.0.0.1")

    mock_cq = MagicMock()
    mock_cq.run = RunIdentity(1, 0, 1)  # step 同，但下方 URL 不同 → 非幂等，触发重启

    with (
        patch.object(db_tasks, "query_task", return_value=db_task),
        patch("app.services.run_control.service.inference_service") as mock_inference,
        patch("app.services.run_control.service.stream_service") as mock_stream,
        patch("app.services.run_control.service.recording_service"),
        patch("app.services.run_control.service.ClientQueues"),
        patch.object(client_service, "set"),  # set 已上移 RunControlService：拦真实注册，防污染全局表
        patch.object(client_service, "has_client", return_value=True),
        patch.object(client_service, "get", return_value=mock_cq),
        patch.object(
            client_service, "remove", return_value={"removed": True, "error": None}
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
# Test 3: 同 task 的 start 和 terminate 并发 → 串行
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_and_terminate_serialized():
    """start 持锁段还没走完时到达的 terminate，要等 start 做完才开始拆（stop_stream）。

    start 在持锁段里（start_workflow）停 0.2s；terminate 在此期间发出。经 lock_for 串行时
    事件序必为 enter → exit → stop；锁失效则 terminate 的 stop_stream 插在中间。
    """
    db_task = _make_db_task(task_id=1, source_ip="10.0.0.1")
    events = []

    def slow_start_workflow(cq):
        events.append("start_workflow:enter")
        time.sleep(0.2)
        events.append("start_workflow:exit")
        return True

    with (
        patch.object(db_tasks, "query_task", return_value=db_task),
        patch("app.services.run_control.service.inference_service") as mock_inference,
        patch("app.services.run_control.service.stream_service") as mock_stream,
        patch("app.services.run_control.service.recording_service"),
        patch("app.services.run_control.service.ClientQueues", side_effect=_new_cq),
    ):
        mock_inference.start_workflow.side_effect = slow_start_workflow
        mock_inference.resolve_stage.return_value = "0"
        mock_inference.stop_workflow.return_value = []
        mock_stream.stop_stream.side_effect = lambda task_id: events.append("stop_stream")

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            start = asyncio.create_task(
                ac.post("/api/start", json={"task_id": 1, "rtsp_url": "rtsp://test/stream"})
            )
            # 等 start 进入持锁段（此时 CQ 已登记，terminate 查得到 run）再发 terminate
            while "start_workflow:enter" not in events:
                await asyncio.sleep(0.005)
            terminate = await ac.post("/api/terminate", json={"task_id": 1})
            start_resp = await start

    assert start_resp.status_code == 200
    assert terminate.status_code == 200
    assert terminate.json()["status"] == "success"
    assert events == ["start_workflow:enter", "start_workflow:exit", "stop_stream"]


# ---------------------------------------------------------------------------
# Test 4: 不同 task 互不阻塞
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_different_tasks_not_blocked():
    """不同 task_id → 不同 lock_for：两个 start 能同时处在各自的持锁段里。

    start_workflow（持锁段内）过一道 2 方 Barrier：两边都进来才放行。若两者被同一把锁
    串行，先进的那个等不到另一个，Barrier 超时 → start_workflow 抛 → 该请求非 200。
    """
    tasks = {
        1: _make_db_task(task_id=1, source_ip="10.0.0.1"),
        2: _make_db_task(task_id=2, source_ip="10.0.0.2"),
    }

    barrier = threading.Barrier(2, timeout=2.0)

    def rendezvous(cq):
        barrier.wait()
        return True

    with (
        patch.object(db_tasks, "query_task", side_effect=tasks.get),  # 按 task_id 取，不依赖到达顺序
        patch("app.services.run_control.service.inference_service") as mock_inference,
        patch("app.services.run_control.service.stream_service") as mock_stream,
        patch("app.services.run_control.service.recording_service"),
        patch("app.services.run_control.service.ClientQueues", side_effect=_new_cq),
    ):
        mock_inference.start_workflow.side_effect = rendezvous
        mock_inference.resolve_stage.return_value = "0"
        mock_inference.stop_workflow.return_value = []

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            results = await asyncio.gather(
                ac.post("/api/start", json={"task_id": 1, "rtsp_url": "rtsp://a/stream"}),
                ac.post("/api/start", json={"task_id": 2, "rtsp_url": "rtsp://b/stream"}),
            )

    assert [r.status_code for r in results] == [200, 200]
    assert sorted(c.kwargs["task_id"] for c in mock_stream.start_stream.call_args_list) == [1, 2]


# ---------------------------------------------------------------------------
# Test 5: terminate 获取 per-task 锁
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "request_kwargs",
    [
        {"json": {"task_id": 1}},              # 首选：body 传 task_id，直查运行键
        {"params": {"client_id": "10.0.0.1"}},  # 兼容期：?client_id=（source_ip）扫描回 run
    ],
    ids=["body_task_id", "query_client_id"],
)
async def test_terminate_uses_lock(request_kwargs):
    """terminate 解析到 run → stop_run 持 lock_for(task_id)、stop_workflow(cq)。"""
    mock_cq = MagicMock()
    mock_cq.run = RunIdentity(1, 0, 1)

    with (
        patch("app.services.run_control.service.inference_service") as mock_inference,
        patch("app.services.run_control.service.stream_service") as mock_stream,
        patch("app.services.run_control.service.recording_service"),
        patch.object(client_service, "find_by_source_ip", return_value=mock_cq),
        patch.object(client_service, "get", return_value=mock_cq),
        patch.object(client_service, "has_client", return_value=True),
        patch.object(
            client_service, "remove", return_value={"removed": True, "error": None}
        ),
    ):
        mock_inference.stop_workflow.return_value = []
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post("/api/terminate", **request_kwargs)

        assert r.status_code == 200
        mock_stream.stop_stream.assert_called_once_with(1)
        mock_inference.stop_workflow.assert_called_once_with(mock_cq)

    # 验证真实的 per-task 锁已按 int task_id 创建
    assert 1 in client_service._task_locks


# ---------------------------------------------------------------------------
# Test 6: start 的 DB 边界（不起任何编排）
# ---------------------------------------------------------------------------


def _db_down(task_id):
    """走真实 query_task、让会话本身失败：503 detail 就是 db 层包出来的 DatabaseError 原文。"""
    with patch.object(db_tasks, "SessionLocal", side_effect=SQLAlchemyError("boom")):
        return _real_query_task(task_id)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "query_task, status, body",
    [
        (
            lambda task_id: None,
            404,
            {"error": "Resource not found", "detail": "Task 7 not found",
             "resource_type": "Task", "resource_id": "7"},
        ),
        (
            lambda task_id: _make_db_task(task_id=task_id, source_ip=""),
            400,
            {"error": "Validation error", "detail": "Task source_ip is required",
             "field": "source_ip"},
        ),
        (
            _db_down,
            503,
            {"error": "Database unavailable", "detail": "Database error: Failed to query task 7 [retryable]",
             "retryable": True},
        ),
    ],
    ids=["task_missing", "source_ip_empty", "db_down"],
)
async def test_start_db_boundary(query_task, status, body):
    """任务不存在 404 / source_ip 为空 400 / DB 失败 503；三者都不进 start_run。"""
    with (
        patch.object(db_tasks, "query_task", side_effect=query_task),
        patch("app.routers.api.run_control_service") as mock_rc,
    ):
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as ac:
            r = await ac.post("/api/start", json={"task_id": 7, "rtsp_url": "rtsp://x/s"})

    assert r.status_code == status
    assert r.json() == body
    mock_rc.start_run.assert_not_called()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
