"""`POST /lab-f3m8/label-probs` 与 lab 任务清单的 `offline_steps`。"""

import numpy as np
import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.types.temporal import LabelProbs, TemporalEvent, TemporalSegment
from app.main import app
from app.routers import lab as lab_router
from app.storage import inference as inference_store
from factories import seed_hls_segments, make_run

T0 = 1_700_000_000
_TS0_US = T0 * 1_000_000
_LABELS = ("idle", "long_brush_insert", "flush")


def _seed_gapped_raw(task_id=1, step_id=2):
    """raw 轨三段各 10s，第二、三段之间停 10s。媒体轴 0~30000ms。"""
    seed_hls_segments(task_id, step_id, [
        _TS0_US, _TS0_US + 10_000_000, _TS0_US + 30_000_000,
    ])


def _probs(ts, rows):
    return LabelProbs(ts=np.asarray(ts, dtype=np.float64), probs=np.asarray(rows), labels=_LABELS)


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_probs_by_class_on_media_axis(client, tmp_storage):
    _seed_gapped_raw()
    inference_store.write_label_probs(make_run(1, 2), _probs(
        [T0 + 1.0, T0 + 12.5, T0 + 25.0],  # 第三帧落在停顿里 → 吸到第三段段首
        [[0.9, 0.05, 0.05], [0.1, 0.8, 0.1], [0.2, 0.2, 0.6]],
    ))

    r = await client.post("/lab-f3m8/label-probs", json={"task_id": 1, "step_id": 2})

    assert r.status_code == 200
    d = r.json()
    assert d["labels"] == list(_LABELS)
    assert d["media_ms"] == [1_000, 12_500, 20_000]
    assert d["media_duration_ms"] == 30_000
    assert len(d["probs"]) == 3 and all(len(row) == 3 for row in d["probs"])  # [C][T]
    assert d["probs"][1] == pytest.approx([0.05, 0.8, 0.2], abs=1e-3)       # long_brush_insert 这一类


@pytest.mark.asyncio
async def test_no_probs_yields_empty_arrays(client, tmp_storage):
    _seed_gapped_raw()
    r = await client.post("/lab-f3m8/label-probs", json={"task_id": 1, "step_id": 2})
    assert r.status_code == 200
    d = r.json()
    assert (d["labels"], d["media_ms"], d["probs"]) == ([], [], [])


@pytest.mark.asyncio
async def test_track_without_segments_is_404(client, tmp_storage):
    _seed_gapped_raw()
    r = await client.post(
        "/lab-f3m8/label-probs", json={"task_id": 1, "step_id": 2, "track": "processed"},
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_missing_identity_is_422(client, tmp_storage):
    r = await client.post("/lab-f3m8/label-probs", json={"task_id": 1})
    assert r.status_code == 422


def test_storage_task_item_carries_offline_steps(tmp_storage):
    """offline_steps = 有分段事实（step 2）或有逐帧概率（step 4）的 step；只有打点（step 3）不算。

    判定本身的全部分支见 test_storage_inference::TestQueryHasOfflineResults。
    """
    _seed_gapped_raw(task_id=1, step_id=2)
    seed_hls_segments(1, 3, [_TS0_US])
    seed_hls_segments(1, 4, [_TS0_US])
    inference_store.write_temporal(make_run(1, 2), [
        TemporalSegment(producer="P", label="flush", start=T0 + 1.0, end=T0 + 2.0),
    ])
    inference_store.write_temporal(make_run(1, 3), [
        TemporalEvent(producer="op", signal="s", value=1, ts=T0 + 1.0),
    ])
    inference_store.write_label_probs(make_run(1, 4), _probs([T0 + 1.0], [[1.0, 0.0, 0.0]]))

    runs_ = [make_run(1, s) for s in (2, 3, 4)]
    item = lab_router._storage_task_to_item(1, runs_)

    assert item.raw_steps == [2, 3, 4]
    assert item.run_ids == {r.step_id: r.run_id for r in runs_}
    assert item.offline_steps == [2, 4]
    # 跨 run 取 raw 轨：最早段起点 / 最晚段尾（step 2 末段 T0+30s 起、EXTINF 10s）
    assert item.start_time == T0 * 1000
    assert item.updated_time == (T0 + 40) * 1000
