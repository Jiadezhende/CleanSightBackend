"""送标任务清单接口（GET /lab-f3m8/tasks）测试。

落盘约定：`{root}/{task_id}/{step_id}/{run_id}/hls/`（`app.storage.hls` 域）；存储根由 conftest 的
`tmp_storage` fixture 指到临时目录（改的是 settings.storage_dir 单一真源）。
"""

from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app
from doubles import FakeDB
from factories import seed_hls_segments


@pytest.mark.asyncio
async def test_lab_tasks_list_returns_raw_steps(monkeypatch, tmp_storage):
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
    db = FakeDB(rows)
    monkeypatch.setattr(lab_router, "get_db", lambda: iter([db]))

    seed_hls_segments(101, 2, [1_700_000_000_000_000])

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


def _force_storage_mode(monkeypatch):
    from app.routers import lab as lab_router

    monkeypatch.setattr(
        lab_router.lab_config, "get_task_source", lambda: "storage"
    )
    # DB 不应被触碰：若调用 get_db 直接炸，证明走的是 storage 分支
    def _boom():
        raise AssertionError("storage mode must not touch the DB")

    monkeypatch.setattr(lab_router, "get_db", _boom)


@pytest.mark.asyncio
async def test_storage_mode_lists_tasks_with_raw_segments(monkeypatch, tmp_storage):
    _force_storage_mode(monkeypatch)

    # task 101: 两个 step 都有 raw 段
    seed_hls_segments(101, 1, [1_700_000_000_000_000])
    seed_hls_segments(101, 2, [1_700_000_005_000_000])
    # task 101 step 3: 建了目录没写成段 → 送标清单里不该出现（list_step_ids 不过滤空 step）
    (tmp_storage / "101" / "3" / "hls").mkdir(parents=True)
    # task 202: 只有 processed 段，没有 raw → 不应入选
    seed_hls_segments(202, 1, [1_700_000_000_000_000], track="processed")
    # 非数字目录（.lab_exports、config 文件）应被跳过
    (tmp_storage / ".lab_exports").mkdir()
    (tmp_storage / "lab_runtime_config.json").write_text("{}")

    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/lab-f3m8/tasks")

    assert resp.status_code == 200
    payload = resp.json()
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
    seed_hls_segments(301, 1, [1_700_000_001_000_000])
    seed_hls_segments(302, 1, [1_700_000_002_000_000])
    seed_hls_segments(303, 1, [1_700_000_003_000_000])

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
