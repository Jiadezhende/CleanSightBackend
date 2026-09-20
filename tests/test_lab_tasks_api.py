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


# ---------------------------------------------------------------------------
# 存储模式（task_source="storage"）：直接枚举磁盘，不碰 DB
# ---------------------------------------------------------------------------


def _make_raw_segment(base: "object", task_id: int, step_id: int, ts_us: int):
    step_dir = base / str(task_id) / str(step_id)
    step_dir.mkdir(parents=True, exist_ok=True)
    (step_dir / f"raw_segment_{ts_us}.mp4").write_bytes(b"x")


def _force_storage_mode(monkeypatch, tmp_path):
    from app.routers import lab as lab_router

    monkeypatch.setattr(lab_router, "get_default_base_dir", lambda: tmp_path)
    monkeypatch.setattr(
        lab_router.lab_config, "get_task_source", lambda: "storage"
    )
    # DB 不应被触碰：若调用 get_db 直接炸，证明走的是 storage 分支
    def _boom():
        raise AssertionError("storage mode must not touch the DB")

    monkeypatch.setattr(lab_router, "get_db", _boom)


@pytest.mark.asyncio
async def test_storage_mode_lists_tasks_with_raw_segments(monkeypatch, tmp_path):
    _force_storage_mode(monkeypatch, tmp_path)

    # task 101: 两个 step 都有 raw 段
    _make_raw_segment(tmp_path, 101, 1, 1_700_000_000_000_000)
    _make_raw_segment(tmp_path, 101, 2, 1_700_000_005_000_000)
    # task 202: 只有 processed 段，没有 raw → 不应入选
    proc_dir = tmp_path / "202" / "1"
    proc_dir.mkdir(parents=True)
    (proc_dir / "processed_segment_1700000000000000.mp4").write_bytes(b"x")
    # 非数字目录（.lab_exports、config 文件）应被跳过
    (tmp_path / ".lab_exports").mkdir()
    (tmp_path / "lab_runtime_config.json").write_text("{}")

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/lab-f3m8/tasks")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["total"] == 1
    item = payload["tasks"][0]
    assert item["task_id"] == 101
    assert item["raw_steps"] == [1, 2]
    assert item["has_raw_segments"] is True
    # 占位字段：step 留空、status=unknown、ip 为空
    assert item["step_id"] is None
    assert item["current_step"] is None
    assert item["status"] == "unknown"
    assert item["source_ip"] is None
    assert item["has_current_step_raw"] is False
    # updated_time/start_time 从段 ts_ms 推导
    assert item["start_time"] == 1_700_000_000_000
    assert item["updated_time"] == 1_700_000_005_000


@pytest.mark.asyncio
async def test_storage_mode_sort_paginate_and_filter(monkeypatch, tmp_path):
    _force_storage_mode(monkeypatch, tmp_path)

    # 三个 task，updated_time 递增：301 < 302 < 303
    _make_raw_segment(tmp_path, 301, 1, 1_700_000_001_000_000)
    _make_raw_segment(tmp_path, 302, 1, 1_700_000_002_000_000)
    _make_raw_segment(tmp_path, 303, 1, 1_700_000_003_000_000)

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 排序 updated_time desc：303, 302, 301
        resp = await client.get("/lab-f3m8/tasks")
        ids = [t["task_id"] for t in resp.json()["tasks"]]
        assert ids == [303, 302, 301]

        # 分页：limit=1 offset=1 → 第二名 302
        resp = await client.get("/lab-f3m8/tasks", params={"limit": 1, "offset": 1})
        page = resp.json()
        assert page["total"] == 3
        assert [t["task_id"] for t in page["tasks"]] == [302]

        # q 子串过滤 task_id
        resp = await client.get("/lab-f3m8/tasks", params={"q": "302"})
        filtered = resp.json()
        assert filtered["total"] == 1
        assert filtered["tasks"][0]["task_id"] == 302
