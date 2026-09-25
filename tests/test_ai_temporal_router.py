"""`POST /ai/temporal`：分段事实读取 + 墙钟 → 媒体刻度换算。"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.domain.temporal import TemporalEvent, TemporalSegment
from app.main import app
from app.storage import inference as inference_store
from factories import seed_hls_segments

T0 = 1_700_000_000  # 首段墙钟（秒）
_TS0_US = T0 * 1_000_000


def _seed_gapped_raw(task_id=1, step_id=2):
    """raw 轨三段各 10s：[T0, T0+10)、[T0+10, T0+20)，停 10s，[T0+30, T0+40)。媒体轴 0~30000ms。"""
    seed_hls_segments(task_id, step_id, [
        _TS0_US, _TS0_US + 10_000_000, _TS0_US + 30_000_000,
    ])


def _seg(label, start, end, conf=0.9, producer="CleanMSTCNBiLSTMSegmenter"):
    return TemporalSegment(producer=producer, label=label, start=start, end=end, conf=conf)


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _body(**kw):
    return {"task_id": 1, "step_id": 2, "type": "segment", **kw}


@pytest.mark.asyncio
async def test_segments_on_media_axis_sorted_and_gap_snapped(client, tmp_storage):
    _seed_gapped_raw()
    inference_store.write_temporal(1, 2, [
        _seg("flush", T0 + 25.0, T0 + 31.5),          # 起点落在停顿里 → 吸到第三段段首 20000
        _seg("long_brush_insert", T0 + 2.5, T0 + 12.0),
        TemporalEvent(producer="op", signal="state", value=1, ts=T0 + 1.0),  # 不是分段，不出
    ])

    r = await client.post("/ai/temporal", json=_body())

    assert r.status_code == 200
    d = r.json()
    assert d["track"] == "raw"
    assert d["media_duration_ms"] == 30_000
    assert [(i["label"], i["start_media_ms"], i["end_media_ms"]) for i in d["items"]] == [
        ("long_brush_insert", 2_500, 12_000),
        ("flush", 20_000, 21_500),
    ]
    assert d["items"][0]["producer"] == "CleanMSTCNBiLSTMSegmenter"
    assert d["items"][0]["conf"] == pytest.approx(0.9)


@pytest.mark.asyncio
async def test_no_result_yields_empty_items(client, tmp_storage):
    _seed_gapped_raw()
    r = await client.post("/ai/temporal", json=_body())
    assert r.status_code == 200
    assert r.json()["items"] == []


@pytest.mark.asyncio
async def test_track_without_segments_is_404(client, tmp_storage):
    _seed_gapped_raw()  # 只铺了 raw
    r = await client.post("/ai/temporal", json=_body(track="processed"))
    assert r.status_code == 404
    assert r.json()["resource_type"] == "Segments"


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [
    {"step_id": 2, "type": "segment"},                          # 缺 task_id
    {"task_id": 1, "step_id": 2},                               # 缺 type
    {"task_id": 1, "step_id": 2, "type": "event"},              # 未开放的 type
    {"task_id": 1, "step_id": 2, "type": "segment", "track": "x"},
])
async def test_invalid_body_is_422(client, tmp_storage, body):
    r = await client.post("/ai/temporal", json=body)
    assert r.status_code == 422
