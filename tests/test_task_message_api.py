from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


class _FakeQuery:
    def __init__(self, row):
        self._row = row

    def filter(self, *_args, **_kwargs):
        return self

    def first(self):
        return self._row


class _FakeDB:
    def __init__(self, row):
        self._row = row

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._row)

    def close(self):
        return None


@pytest.mark.asyncio
async def test_task_message_since_seq_validation():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/task/message/1?since_seq=-1")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_task_message_task_not_found(monkeypatch):
    from app.routers import task as task_router

    monkeypatch.setattr(task_router, "get_db", lambda: iter([_FakeDB(row=None)]))
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/task/message/1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_task_message_task_exists_but_not_running(monkeypatch):
    from app.routers import task as task_router

    row = SimpleNamespace(task_id=1, source_ip="client-1")
    monkeypatch.setattr(task_router, "get_db", lambda: iter([_FakeDB(row=row)]))

    fake_manager = MagicMock()
    fake_manager.has_client.return_value = False
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

    row = SimpleNamespace(task_id=1, source_ip="client-1")
    monkeypatch.setattr(task_router, "get_db", lambda: iter([_FakeDB(row=row)]))

    cq = MagicMock()
    cq.get_task_id.return_value = 1
    cq.get_task_alarm_message.return_value = {
        "task_id": 1,
        "max_seq": 2,
        "signals_10s": {
            "BUBBLE": {"active": True, "hit_count": 1, "max_conf": 0.8},
            "BENDING": {"active": False, "hit_count": 0, "max_conf": 0.0},
            "TASK_SETTLEMENT": {"active": False, "hit_count": 0, "max_conf": 0.0},
        },
        "alarms": [
            {
                "seq": 2,
                "mode": "REALTIME",
                "metric": "BUBBLE",
                "level": "high",
                "message": "bubble high",
                "count": 1,
                "ts": 1,
            }
        ],
    }

    fake_manager = MagicMock()
    fake_manager.has_client.return_value = True
    fake_manager.get_client.return_value = cq
    monkeypatch.setattr(task_router, "client_manager", fake_manager)

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/task/message/1?since_seq=1")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["max_seq"] == 2
    assert payload["alarms"][0]["seq"] == 2
    cq.get_task_alarm_message.assert_called_once_with(task_id=1, since_seq=1)
