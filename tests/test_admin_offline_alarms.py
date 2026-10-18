"""`GET /admin-f3m8/offline/alarms/{task_id}/{step_id}`（过渡期）：盘上告警 + 墙钟 → 媒体刻度，区分没核对过。"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.storage import inference as inference_store
from app.types.alarm import ALARM_MODE_SETTLEMENT, Alarm, AlarmMetric, AlarmType
from factories import make_run, seed_hls_segments

T0 = 1_700_000_000
_TS0_MS = T0 * 1000
URL = "/admin-f3m8/offline/alarms/1/2"


def _alarm(ts, metric=AlarmMetric.STEP_MISSING, rule_id="clean.flush.missing"):
    return Alarm(alarm_type=AlarmType.PROCESS_VIOLATION, alarm_level="warning", alarm_message="m",
                 metric=metric, metadata={"rule_id": rule_id, "evidence_s": 0.0},
                 mode=ALARM_MODE_SETTLEMENT, stage="CLEAN", seq=0, timestamp=ts)


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_alarms_on_media_axis(client, tmp_storage):
    seed_hls_segments(1, 2, [_TS0_MS, _TS0_MS + 10_000, _TS0_MS + 30_000])   # 停 10s：媒体轴 0~30000
    inference_store.write_alarms(make_run(1, 2), [
        _alarm(T0 + 2.5),
        _alarm(T0 + 35.0, AlarmMetric.STEP_WAIT, "clean.flush.wait"),        # 停顿后：媒体 25000
    ])
    d = (await client.get(URL)).json()
    assert d["checked"] is True and d["track"] == "raw"
    assert [(i["rule_id"], i["metric"], i["media_ms"]) for i in d["items"]] == [
        ("clean.flush.missing", "STEP_MISSING", 2_500),
        ("clean.flush.wait", "STEP_WAIT", 25_000),
    ]
    assert set(d["items"][0]) == {"rule_id", "metric", "alarm_level", "alarm_message", "media_ms"}


@pytest.mark.asyncio
async def test_unchecked_vs_checked_empty(client, tmp_storage):
    seed_hls_segments(1, 2, [_TS0_MS])
    assert (await client.get(URL)).json() | {"run_id": None} == {
        "task_id": 1, "step_id": 2, "run_id": None, "track": "raw", "checked": False, "items": [],
    }
    inference_store.write_alarms(make_run(1, 2), [])
    d = (await client.get(URL)).json()
    assert (d["checked"], d["items"]) == (True, [])


@pytest.mark.asyncio
async def test_track_without_segments_is_404(client, tmp_storage):
    seed_hls_segments(1, 2, [_TS0_MS])
    assert (await client.get(URL, params={"track": "processed"})).status_code == 404
