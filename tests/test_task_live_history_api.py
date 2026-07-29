"""
大屏清单接口测试：GET /task/live、GET /task/history

两张清单只出参数、不出 URL，所以断言重点是**参数能不能直接喂给播放端**：
- /live 出的 task_id/source_ip 就是 `WS /ai/video` 的两种入参
- /history 出的 (task_id, step_id, tracks[]) 就是 `/traceback/.../playlist.m3u8` 的入参，
  其中 tracks 必须反映磁盘实况——playlist 的 track 默认 processed，只有 raw 的 step
  照默认打过去就是 404，这是本文件的核心回归点。

DB / 文件系统沿用既有 seam：_FakeDB（同 test_lab_tasks_api）+ tmp_path 造段文件。
"""

import os
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from factories import make_cq


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_args, **_kwargs):
        return self

    def all(self):
        return self._rows


class _FakeDB:
    def __init__(self, rows):
        self._rows = rows
        self.closed = False

    def query(self, *_args, **_kwargs):
        return _FakeQuery(self._rows)

    def close(self):
        self.closed = True


def _install_db(monkeypatch, rows):
    """把 /task/history 的 source_ip 查询接到假 DB。"""
    from app.routers import task as task_router

    db = _FakeDB(rows)
    monkeypatch.setattr(task_router, "get_db", lambda: iter([db]))
    return db


def _install_registry(monkeypatch, cqs):
    """替换活跃注册表快照（决定 /live 出什么、/history 排除谁）。"""
    from app.routers import task as task_router

    runs = {cq.task_id: cq for cq in cqs}
    monkeypatch.setattr(
        task_router.client_manager, "snapshot", lambda: runs, raising=True
    )


def _write_segments(base, task_id, step_id, *, tracks=("raw",), ts_us=1_000_000, mtime=None):
    d = base / str(task_id) / str(step_id)
    d.mkdir(parents=True, exist_ok=True)
    for track in tracks:
        (d / f"{track}_segment_{ts_us}.mp4").write_bytes(b"")
    if mtime is not None:
        os.utime(d, (mtime, mtime))
    return d


async def _get(path):
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


@pytest.fixture
def storage(monkeypatch, tmp_path):
    """把 /task/history 的存储根目录指到隔离临时目录。"""
    from app.routers import task as task_router

    monkeypatch.setattr(task_router, "get_default_base_dir", lambda: tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# GET /task/live
# ---------------------------------------------------------------------------


class TestLiveList:
    @pytest.mark.asyncio
    async def test_empty_when_no_active_run(self, monkeypatch):
        _install_registry(monkeypatch, [])

        resp = await _get("/task/live")

        assert resp.status_code == 200
        assert resp.json() == {"total": 0, "tasks": []}

    @pytest.mark.asyncio
    async def test_returns_ws_params_sorted_by_task_id(self, monkeypatch):
        _install_registry(
            monkeypatch,
            [
                make_cq(task_id=202, step_id=1, source_ip="10.0.0.2"),
                make_cq(task_id=101, step_id=2, source_ip="10.0.0.1"),
            ],
        )

        payload = (await _get("/task/live")).json()

        assert payload["total"] == 2
        # task_id / source_ip 即 WS /ai/video 的两种入参；step_id 供展示当前阶段
        assert payload["tasks"] == [
            {"task_id": 101, "source_ip": "10.0.0.1", "step_id": 2},
            {"task_id": 202, "source_ip": "10.0.0.2", "step_id": 1},
        ]


# ---------------------------------------------------------------------------
# GET /task/history
# ---------------------------------------------------------------------------


class TestHistoryList:
    @pytest.mark.asyncio
    async def test_empty_when_storage_empty(self, monkeypatch, storage):
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        resp = await _get("/task/history")

        assert resp.status_code == 200
        assert resp.json() == {"tasks": []}

    @pytest.mark.asyncio
    async def test_raw_only_step_reports_raw_track(self, monkeypatch, storage):
        """核心回归点：只落了 raw 的 step 必须如实报 tracks=["raw"]。

        前端照 playlist 的默认 track=processed 打过去会 404，只能按这里给的挑。
        """
        _write_segments(storage, 101, 1, tracks=("raw",))
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == [101]
        assert payload["tasks"][0]["steps"] == [
            {
                "step_id": 1,
                "tracks": ["raw"],
                "start_ms": 1000,
                "last_segment_ms": 1000,
            }
        ]

    @pytest.mark.asyncio
    async def test_dual_track_step_reports_both(self, monkeypatch, storage):
        _write_segments(storage, 101, 1, tracks=("raw", "processed"))
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert payload["tasks"][0]["steps"][0]["tracks"] == ["raw", "processed"]

    @pytest.mark.asyncio
    async def test_running_task_is_excluded(self, monkeypatch, storage):
        """磁盘有段但还在跑 → 不算历史（本次「已完成」判定的核心）。"""
        _write_segments(storage, 101, 1, ts_us=1_000_000)
        _write_segments(storage, 202, 1, ts_us=2_000_000)
        _install_registry(monkeypatch, [make_cq(task_id=202, step_id=1)])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == [101]

    @pytest.mark.asyncio
    async def test_task_dir_without_segments_is_dropped(self, monkeypatch, storage):
        (storage / "303" / "1").mkdir(parents=True)  # 目录在但没段 → 点开黑屏
        _write_segments(storage, 101, 1)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == [101]

    @pytest.mark.asyncio
    async def test_task_level_ts_spans_all_steps(self, monkeypatch, storage):
        _write_segments(storage, 101, 1, ts_us=1_000_000)
        _write_segments(storage, 101, 2, ts_us=9_000_000)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        task = (await _get("/task/history")).json()["tasks"][0]

        assert task["start_ms"] == 1000
        assert task["last_segment_ms"] == 9000

    @pytest.mark.asyncio
    async def test_source_ip_filled_from_db(self, monkeypatch, storage):
        _write_segments(storage, 101, 1)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [SimpleNamespace(task_id=101, source_ip="10.0.0.1")])

        payload = (await _get("/task/history")).json()

        assert payload["tasks"][0]["source_ip"] == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_source_ip_null_when_task_absent_from_db(self, monkeypatch, storage):
        _write_segments(storage, 101, 1)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])  # DB 里没这条

        payload = (await _get("/task/history")).json()

        assert payload["tasks"][0]["source_ip"] is None

    @pytest.mark.asyncio
    async def test_db_failure_degrades_instead_of_503(self, monkeypatch, storage):
        """存在性判定来自磁盘，DB 只补 source_ip —— DB 挂了清单照常出。"""
        from app.routers import task as task_router

        def _boom():
            raise RuntimeError("connection refused")

        _write_segments(storage, 101, 1)
        _install_registry(monkeypatch, [])
        monkeypatch.setattr(task_router, "get_db", _boom)

        resp = await _get("/task/history")

        assert resp.status_code == 200
        assert resp.json()["tasks"][0]["source_ip"] is None

    @pytest.mark.asyncio
    async def test_caps_at_ten_newest_first(self, monkeypatch, storage):
        # 12 个任务，段时间戳递增；mtime 同序，保证粗筛也挑到最新的那批
        for i in range(12):
            _write_segments(
                storage, 100 + i, 1, ts_us=(i + 1) * 1_000_000, mtime=1_000 + i
            )
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == list(range(111, 101, -1))

    @pytest.mark.asyncio
    async def test_order_uses_real_segment_ts_not_mtime(self, monkeypatch, storage):
        """mtime 只用于挑候选；最终顺序按真实段时间戳重排。"""
        _write_segments(storage, 101, 1, ts_us=9_000_000, mtime=1_000)  # 段新、mtime 旧
        _write_segments(storage, 202, 1, ts_us=1_000_000, mtime=9_000)  # 段旧、mtime 新
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == [101, 202]
