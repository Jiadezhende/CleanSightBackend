from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._offset = 0
        self._limit = None

    def filter(self, *_args, **_kwargs):
        return self

    def count(self):
        return len(self._rows)

    def order_by(self, *_args, **_kwargs):
        return self

    def offset(self, value):
        self._offset = value
        return self

    def limit(self, value):
        self._limit = value
        return self

    def all(self):
        end = None if self._limit is None else self._offset + self._limit
        return self._rows[self._offset:end]


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._rows)

    def close(self):
        self.closed = True


@pytest.mark.asyncio
async def test_lab_tasks_list_returns_raw_steps(monkeypatch, tmp_path):
    from app.routers import lab as lab_router

    rows = [
        SimpleNamespace(
            task_id=101,
            source_ip="10.0.0.1",
            current_step="2",
            status="completed",
            updated_time=1_700_000_000_000,
            start_time=1_700_000_000_000,
            end_time=1_700_000_010_000,
        )
    ]
    db = _FakeDB(rows)
    monkeypatch.setattr(lab_router, "get_db", lambda: iter([db]))
    monkeypatch.setattr(lab_router, "get_default_base_dir", lambda: tmp_path)

    step_dir = tmp_path / "101" / "2"
    step_dir.mkdir(parents=True)
    (step_dir / "raw_segment_1700000000000000.mp4").write_bytes(b"x")

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/lab-f3m8/tasks")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    assert payload["tasks"][0]["task_id"] == 101
    assert payload["tasks"][0]["step_id"] == 2
    assert payload["tasks"][0]["raw_steps"] == [2]
    assert payload["tasks"][0]["has_raw_segments"] is True
    assert payload["tasks"][0]["has_current_step_raw"] is True
    assert db.closed is True
