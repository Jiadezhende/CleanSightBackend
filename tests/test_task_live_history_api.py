"""
大屏清单接口测试：GET /task/live、GET /task/history

两张清单只出参数、不出 URL，所以断言重点是**参数能不能直接喂给播放端**：
- /live 出的 task_id/source_ip 就是 `WS /ai/video` 的两种入参
- /history 出的 (task_id, step_id, run_id, tracks[]) 就是 `/traceback/.../playlist.m3u8` 的入参，
  其中 tracks 必须反映磁盘实况——playlist 的 track 默认 processed，只有 raw 的 step
  照默认打过去就是 404，这是本文件的核心回归点。

DB / 文件系统沿用既有 seam：`doubles.FakeDB` + `tmp_storage` 里用 `factories.seed_hls_segments`
造段（落盘 `{root}/{task}/{step}/{run_id}/hls/` 并登记进清单）。
"""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from doubles import FakeDB
from factories import make_cq, make_run, seed_hls_segments
from app.storage import runs


# ---------------------------------------------------------------------------
# 测试替身
# ---------------------------------------------------------------------------


def _install_db(monkeypatch, rows):
    """把 /task/history 的 source_ip 查询接到假 DB。"""
    from app.routers import task as task_router

    db = FakeDB(rows)
    monkeypatch.setattr(task_router, "get_db", lambda: iter([db]))
    return db


def _install_registry(monkeypatch, cqs):
    """替换活跃注册表快照（决定 /live 出什么、/history 排除谁）。"""
    from app.routers import task as task_router

    runs = {cq.run.task_id: cq for cq in cqs}
    monkeypatch.setattr(
        task_router.client_service, "snapshot", lambda: runs, raising=True
    )


def _write_segments(task_id, step_id, *, tracks=("raw",), ts_us=1_000_000, run_id=None):
    """每条 track 在该 step 的 run（`make_run`，给了 `run_id` 就按它建）的 `hls/` 下造一段并登记
    进清单。返回 hls 域目录。"""
    make_run(task_id, step_id, run_id=run_id)
    for track in tracks:
        d = seed_hls_segments(task_id, step_id, [ts_us], track=track)
    return d


async def _get(path):
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.get(path)


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
                make_cq(task_id=202, step_id=1, run_id=7, source_ip="10.0.0.2"),
                make_cq(task_id=101, step_id=2, run_id=9, source_ip="10.0.0.1"),
            ],
        )

        payload = (await _get("/task/live")).json()

        assert payload["total"] == 2
        # task_id / source_ip 即 WS /ai/video 的两种入参；step_id 供展示当前阶段
        assert payload["tasks"] == [
            {"task_id": 101, "source_ip": "10.0.0.1", "step_id": 2, "run_id": 9},
            {"task_id": 202, "source_ip": "10.0.0.2", "step_id": 1, "run_id": 7},
        ]


# ---------------------------------------------------------------------------
# GET /task/history
# ---------------------------------------------------------------------------


class TestHistoryList:
    @pytest.mark.asyncio
    async def test_empty_when_storage_empty(self, monkeypatch, tmp_storage):
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        resp = await _get("/task/history")

        assert resp.status_code == 200
        assert resp.json() == {"tasks": []}

    @pytest.mark.asyncio
    async def test_raw_only_step_reports_raw_track(self, monkeypatch, tmp_storage):
        """核心回归点：只落了 raw 的 step 必须如实报 tracks=["raw"]。

        前端照 playlist 的默认 track=processed 打过去会 404，只能按这里给的挑。
        """
        _write_segments(101, 1, tracks=("raw",))
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == [101]
        assert payload["tasks"][0]["steps"] == [
            {
                "step_id": 1,
                "run_id": runs.query(101, 1).run_id,
                "tracks": ["raw"],
                "start_ms": 1000,
                "last_segment_ms": 1000,
            }
        ]

    @pytest.mark.asyncio
    async def test_dual_track_step_reports_both(self, monkeypatch, tmp_storage):
        _write_segments(101, 1, tracks=("raw", "processed"))
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert payload["tasks"][0]["steps"][0]["tracks"] == ["raw", "processed"]

    @pytest.mark.asyncio
    async def test_step_span_takes_union_of_both_tracks(self, monkeypatch, tmp_storage):
        """step 时间跨度取双轨并集：两轨段边界不对齐时，起点、终点可以各来自不同轨。"""
        seed_hls_segments(101, 1, [1_000_000, 5_000_000], track="raw")        # 起点在 raw
        seed_hls_segments(101, 1, [3_000_000, 9_000_000], track="processed")  # 终点在 processed
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        step = (await _get("/task/history")).json()["tasks"][0]["steps"][0]

        assert (step["start_ms"], step["last_segment_ms"]) == (1000, 9000)

    @pytest.mark.asyncio
    async def test_running_task_is_excluded(self, monkeypatch, tmp_storage):
        """磁盘有段但还在跑 → 不算历史（本次「已完成」判定的核心）。"""
        _write_segments(101, 1, ts_us=1_000_000)
        _write_segments(202, 1, ts_us=2_000_000)
        _install_registry(monkeypatch, [make_cq(task_id=202, step_id=1)])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == [101]

    @pytest.mark.asyncio
    async def test_task_dir_without_segments_is_dropped(self, monkeypatch, tmp_storage):
        (tmp_storage / "303" / "1").mkdir(parents=True)  # 目录在但没段 → 点开黑屏
        _write_segments(101, 1)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == [101]

    @pytest.mark.asyncio
    async def test_empty_step_is_dropped_from_a_task_that_has_others(
        self, monkeypatch, tmp_storage
    ):
        """同一个 task 里「有目录没段」的 step 不能进 steps[]。

        `tasks.list_step_ids` 刻意**不过滤空 step**（TTL 要的正是没过滤那档），过滤是
        调用点的责任。漏掉它不会让整条清单消失——只会在大屏多出一个点开黑屏的 step，
        所以上面那条「整个 task 被丢掉」的用例盖不住它。
        """
        _write_segments(101, 1)
        (tmp_storage / "101" / "2").mkdir(parents=True)          # 建了目录没写成段
        (tmp_storage / "101" / "3" / "hls").mkdir(parents=True)  # 连域目录都建了，还是没段
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [s["step_id"] for s in payload["tasks"][0]["steps"]] == [1]

    @pytest.mark.asyncio
    async def test_task_level_time_is_latest_only(self, monkeypatch, tmp_storage):
        """任务级只出 latest_ms，不出 start_ms。

        两个 step 之间可以隔任意长时间，任务级「最早~最晚」跨过中间空档，
        既不是任务时长也不对应可播放范围——时间字段一律压在 step 粒度。
        """
        _write_segments(101, 1, ts_us=1_000_000)
        _write_segments(101, 2, ts_us=9_000_000)  # 与 step 1 隔 8 秒空档
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        task = (await _get("/task/history")).json()["tasks"][0]

        assert task["latest_ms"] == 9000
        assert "start_ms" not in task
        assert "last_segment_ms" not in task
        # step 粒度仍各自给出真实区间
        assert [(s["start_ms"], s["last_segment_ms"]) for s in task["steps"]] == [
            (1000, 1000),
            (9000, 9000),
        ]

    @pytest.mark.asyncio
    async def test_source_ip_filled_from_db(self, monkeypatch, tmp_storage):
        _write_segments(101, 1)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [SimpleNamespace(task_id=101, source_ip="10.0.0.1")])

        payload = (await _get("/task/history")).json()

        assert payload["tasks"][0]["source_ip"] == "10.0.0.1"

    @pytest.mark.asyncio
    async def test_source_ip_null_when_task_absent_from_db(self, monkeypatch, tmp_storage):
        _write_segments(101, 1)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])  # DB 里没这条

        payload = (await _get("/task/history")).json()

        assert payload["tasks"][0]["source_ip"] is None

    @pytest.mark.asyncio
    async def test_db_failure_degrades_instead_of_503(self, monkeypatch, tmp_storage):
        """存在性判定来自磁盘，DB 只补 source_ip —— DB 挂了清单照常出。"""
        from app.routers import task as task_router

        def _boom():
            raise RuntimeError("connection refused")

        _write_segments(101, 1)
        _install_registry(monkeypatch, [])
        monkeypatch.setattr(task_router, "get_db", _boom)

        resp = await _get("/task/history")

        assert resp.status_code == 200
        assert resp.json()["tasks"][0]["source_ip"] is None

    @pytest.mark.asyncio
    async def test_caps_at_ten_newest_first(self, monkeypatch, tmp_storage):
        # 12 个任务，run 开跑时刻递增
        for i in range(12):
            _write_segments(100 + i, 1, ts_us=(i + 1) * 1_000_000, run_id=1_000 + i)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == list(range(111, 101, -1))

    @pytest.mark.asyncio
    async def test_order_is_by_run_start_not_segment_ts(self, monkeypatch, tmp_storage):
        """排序键是列出的 run 的 run_id（开跑时刻），不是最后一段的时刻。"""
        _write_segments(101, 1, ts_us=9_000_000, run_id=1_000)  # 早开跑、段晚
        _write_segments(202, 1, ts_us=1_000_000, run_id=2_000)  # 晚开跑、段早
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == [202, 101]

    @pytest.mark.asyncio
    async def test_unlisted_generations_do_not_count(self, monkeypatch, tmp_storage):
        """同 step 更新但没段的一代（起流即失败）不参与排序，也不该把后面的任务挤出前 10。"""
        _write_segments(999, 1, run_id=100)
        (tmp_storage / "999" / "1" / "9000").mkdir()      # 更新的一代：空 run 目录，粗排上界最高
        for i in range(10):
            _write_segments(100 + i, 1, run_id=1_000 + i)
        _install_registry(monkeypatch, [])
        _install_db(monkeypatch, [])

        payload = (await _get("/task/history")).json()

        assert [t["task_id"] for t in payload["tasks"]] == list(range(109, 99, -1))
