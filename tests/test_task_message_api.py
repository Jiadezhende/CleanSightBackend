from unittest.mock import MagicMock

import pytest
from factories import make_alarm
from httpx import ASGITransport, AsyncClient

from app.main import app


@pytest.mark.asyncio
async def test_task_message_since_seq_validation():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/task/message/1?since_seq=-1")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_task_message_task_not_in_memory_returns_empty(monkeypatch):
    """
    task_id 在内存中查不到（任务已结束 / 从未存在）时返回 200 + 空 payload，
    而非 404。

    原因：本接口供前端 1~2 Hz 轮询，若对查不到的 task_id 返回 404，前端轮询一个
    刚结束的任务会持续打出 404，被网关反扫描机制（gateway.py，404/405 累计触发
    自动封禁）误判为路径枚举扫描而封禁 IP。统一返回空 payload 即可避开该机制。
    代价：调用方无法区分"任务已结束"与"task_id 非法"，这是刻意取舍。
    """
    from app.routers import task as task_router

    fake_manager = MagicMock()
    fake_manager.get.return_value = None
    monkeypatch.setattr(task_router, "client_manager", fake_manager)

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/task/message/1")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["task_id"] == 1
    assert payload["max_seq"] == 0
    assert payload["alarms"] == []


@pytest.mark.asyncio
async def test_task_message_running_returns_increment(monkeypatch):
    from app.routers import task as task_router

    from app.domain.alarm import AlarmMetric

    # 装配层的 metric 映射用 config 单一真源（lazy YAML），此处只验端点串起装配 +
    # 原子入口调用；alarm/max_seq 不依赖映射，signals_10s schema 由装配层单测覆盖。
    alarm = make_alarm(metric=AlarmMetric.BUBBLE, mode="REALTIME", seq=2, timestamp=1.0)

    cq = MagicMock()
    cq.task_id = 1
    cq.get_alarm_snapshot.return_value = ([alarm], 2)
    cq.get_slide_window_summary.return_value = {
        "bubble": {"active": True, "hit_count": 1, "max_conf": 0.8}
    }

    fake_manager = MagicMock()
    fake_manager.get.return_value = cq
    monkeypatch.setattr(task_router, "client_manager", fake_manager)

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/task/message/1?since_seq=1")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["max_seq"] == 2
    assert payload["alarms"][0]["seq"] == 2
    fake_manager.get.assert_called_once_with(1)
    cq.get_alarm_snapshot.assert_called_once_with(1)
