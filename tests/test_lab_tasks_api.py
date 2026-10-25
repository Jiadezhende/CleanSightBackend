"""送标任务清单接口（GET /lab/tasks）测试。

落盘约定：`{root}/{task_id}/{step_id}/{run_id}/hls/`（`app.storage.hls` 域）；存储根由 conftest 的
`tmp_storage` fixture 指到临时目录（改的是 settings.storage_dir 单一真源）。
"""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.db import tasks as db_tasks
from app.main import app
from app.types.exceptions import DatabaseError
from factories import seed_hls_segments


def _row(task_id, *, source_ip="10.0.0.1", status="completed", updated_time=1_700_000_000_000, current_step="2"):
    return SimpleNamespace(
        task_id=task_id, source_ip=source_ip, current_step=current_step, status=status,
        updated_time=updated_time, start_time=updated_time, end_time=updated_time + 10_000,
    )


def _db_rows(monkeypatch, rows):
    """平台库替身：`query_tasks` 只返回被问到、且在 rows 里的 task；记录每次被问的 id。"""
    asked = []

    def _query(task_ids):
        asked.append(sorted(task_ids))
        return {r.task_id: r for r in rows if r.task_id in task_ids}

    monkeypatch.setattr(db_tasks, "query_tasks", _query)
    return asked


async def _get_tasks(**params):
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/lab/tasks", params=params)
    assert resp.status_code == 200
    return resp.json()


# ---------------------------------------------------------------------------
# 平台库模式（task_source="db"，默认）：磁盘为准 + 平台库过滤补字段，库不通退回仅磁盘
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_db_mode_lists_disk_tasks_enriched_by_db(monkeypatch, tmp_storage):
    seed_hls_segments(101, 2, [1_700_000_000_000])
    asked = _db_rows(monkeypatch, [_row(101)])

    payload = await _get_tasks()

    assert asked == [[101]]                 # 只问磁盘上有 raw 段的任务
    assert payload["source"] == "db"
    assert payload["total"] == 1
    item = payload["tasks"][0]
    assert (item["task_id"], item["source_ip"], item["status"]) == (101, "10.0.0.1", "completed")
    assert item["step_id"] == 2
    assert item["raw_steps"] == [2]
    assert item["has_raw_segments"] is True
    assert item["has_current_step_raw"] is True


@pytest.mark.asyncio
async def test_db_mode_drops_tasks_without_recordings_or_db_row(monkeypatch, tmp_storage):
    seed_hls_segments(101, 1, [1_700_000_000_000])      # 盘上有、库里有 → 列
    seed_hls_segments(102, 1, [1_700_000_000_000])      # 盘上有、库里没有 → 不列
    seed_hls_segments(103, 1, [1_700_000_000_000], track="processed")  # 只有 processed → 不算有录像
    _db_rows(monkeypatch, [_row(101), _row(103), _row(104)])          # 104 库里有、盘上没有 → 不列

    payload = await _get_tasks()

    assert [t["task_id"] for t in payload["tasks"]] == [101]
    assert payload["total"] == 1


@pytest.mark.asyncio
async def test_db_mode_sort_paginate_and_filter(monkeypatch, tmp_storage):
    for tid in (301, 302, 303):
        seed_hls_segments(tid, 1, [1_700_000_000_000])
    _db_rows(monkeypatch, [
        _row(301, source_ip="192.168.1.1", status="running", updated_time=300),
        _row(302, source_ip="10.0.0.2", status="completed", updated_time=200),
        _row(303, source_ip="10.0.0.3", status="RUNNING", updated_time=200),
    ])

    # updated_time desc，同值再按 task_id desc
    assert [t["task_id"] for t in (await _get_tasks())["tasks"]] == [301, 303, 302]
    page = await _get_tasks(limit=1, offset=1)
    assert (page["total"], [t["task_id"] for t in page["tasks"]]) == (3, [303])
    # q：task_id / source_ip / status 子串，大小写不敏感，两端空白忽略
    assert [t["task_id"] for t in (await _get_tasks(q=" 192.168 "))["tasks"]] == [301]
    assert [t["task_id"] for t in (await _get_tasks(q="run"))["tasks"]] == [301, 303]
    assert [t["task_id"] for t in (await _get_tasks(q="302"))["tasks"]] == [302]


@pytest.mark.asyncio
async def test_db_mode_falls_back_to_storage_when_db_down(monkeypatch, tmp_storage):
    seed_hls_segments(101, 1, [1_700_000_000_000])

    def _down(_task_ids):
        raise DatabaseError(message="Failed to query tasks", retryable=True)

    monkeypatch.setattr(db_tasks, "query_tasks", _down)

    payload = await _get_tasks()

    assert payload["source"] == "storage"
    assert [t["task_id"] for t in payload["tasks"]] == [101]
    assert payload["tasks"][0]["source_ip"] is None    # 退回仅磁盘：平台库字段为空


# ---------------------------------------------------------------------------
# 存储模式（task_source="storage"）：直接枚举磁盘，不碰 DB
# ---------------------------------------------------------------------------


def _force_storage_mode(monkeypatch):
    from app.routers import lab as lab_router

    monkeypatch.setattr(
        lab_router.lab_config, "get_task_source", lambda: "storage"
    )
    # DB 不应被触碰：若查 DB 直接炸，证明走的是 storage 分支
    def _boom(*_args, **_kwargs):
        raise AssertionError("storage mode must not touch the DB")

    monkeypatch.setattr(db_tasks, "query_tasks", _boom)


@pytest.mark.asyncio
async def test_storage_mode_lists_tasks_with_raw_segments(monkeypatch, tmp_storage):
    _force_storage_mode(monkeypatch)

    # task 101: 两个 step 都有 raw 段
    seed_hls_segments(101, 1, [1_700_000_000_000])
    seed_hls_segments(101, 2, [1_700_000_005_000])
    # task 101 step 3: 建了目录没写成段 → 送标清单里不该出现（list_step_ids 不过滤空 step）
    (tmp_storage / "101" / "3" / "hls").mkdir(parents=True)
    # task 202: 只有 processed 段，没有 raw → 不应入选
    seed_hls_segments(202, 1, [1_700_000_000_000], track="processed")
    # 非数字目录（.lab_exports、config 文件）应被跳过
    (tmp_storage / ".lab_exports").mkdir()
    (tmp_storage / "lab_runtime_config.json").write_text("{}")

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/lab/tasks")

    assert resp.status_code == 200
    payload = resp.json()
    assert payload["source"] == "storage"
    assert payload["total"] == 1
    item = payload["tasks"][0]
    assert item["task_id"] == 101
    assert item["raw_steps"] == [1, 2]     # step 3 有目录没段 → 不进清单
    assert item["has_raw_segments"] is True
    # 占位字段：step 留空、status=unknown、ip 为空
    assert item["step_id"] is None
    assert item["current_step"] is None
    assert item["status"] == "unknown"
    assert item["source_ip"] is None
    assert item["has_current_step_raw"] is False
    # start_time = 首段**起点**；updated_time = 末段**段尾**（ts + EXTINF）。
    # 末端取段尾而不是段起点：后者会漏掉最后一段自身的长度，列表里的"最后更新"就恒比实际
    # 早一个段长（seed_hls_segments 缺省 EXTINF 10s，故 ...005_000 → ...015_000）。
    assert item["start_time"] == 1_700_000_000_000
    assert item["updated_time"] == 1_700_000_005_000 + 10_000


@pytest.mark.asyncio
async def test_storage_mode_sort_paginate_and_filter(monkeypatch, tmp_storage):
    _force_storage_mode(monkeypatch)

    # 三个 task，updated_time 递增：301 < 302 < 303
    seed_hls_segments(301, 1, [1_700_000_001_000])
    seed_hls_segments(302, 1, [1_700_000_002_000])
    seed_hls_segments(303, 1, [1_700_000_003_000])

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # 排序 updated_time desc：303, 302, 301
        resp = await client.get("/lab/tasks")
        ids = [t["task_id"] for t in resp.json()["tasks"]]
        assert ids == [303, 302, 301]

        # 分页：limit=1 offset=1 → 第二名 302
        resp = await client.get("/lab/tasks", params={"limit": 1, "offset": 1})
        page = resp.json()
        assert page["total"] == 3
        assert [t["task_id"] for t in page["tasks"]] == [302]

        # q 子串过滤 task_id
        resp = await client.get("/lab/tasks", params={"q": "302"})
        filtered = resp.json()
        assert filtered["total"] == 1
        assert filtered["tasks"][0]["task_id"] == 302
