"""
追溯路由（/traceback/*, /media/*）端到端测试

覆盖：
- /traceback/alarm/{id}/evidence：返回双轨 URL + keypoints，按 alarm.step_id 定位
- /traceback/task/{id}/playlist.m3u8：必填 step_id，动态 VOD 生成
- /traceback/task/{id}/timeline：必填 step_id，仅返回该 step 的事件
- /media/segment/{token}：合法 token 下载，伪造 token 拒绝
- /media/keypoints/{token}：合法 token 拉 JSON
- detected_at 单位归一（秒 / 毫秒 / 微秒）
- 路径穿越防御

落盘约定：{base_dir}/{task_id}/{step_id}/
"""

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.traceback.media_token import MediaToken


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_task(base_dir: Path, task_id: int, step_id: int, ts_us_list, write_kp=True):
    """造一个任务-步骤的 raw + processed 段 + keypoints + playlist 文件"""
    d = base_dir / str(task_id) / str(step_id)
    d.mkdir(parents=True, exist_ok=True)
    raw_pl_lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10"]
    proc_pl_lines = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:10"]
    for ts_us in ts_us_list:
        (d / f"raw_segment_{ts_us}.mp4").write_bytes(b"\x00" * 16)
        (d / f"processed_segment_{ts_us}.mp4").write_bytes(b"\x00" * 16)
        raw_pl_lines.append("#EXTINF:10.000,")
        raw_pl_lines.append(f"raw_segment_{ts_us}.mp4")
        proc_pl_lines.append("#EXTINF:10.000,")
        proc_pl_lines.append(f"processed_segment_{ts_us}.mp4")
        if write_kp:
            (d / f"keypoints_{ts_us}.json").write_text(
                json.dumps([{"timestamp": ts_us / 1e6, "keypoints": [1, 2, 3]}])
            )
    (d / "raw_playlist.m3u8").write_text("\n".join(raw_pl_lines) + "\n")
    (d / "processed_playlist.m3u8").write_text("\n".join(proc_pl_lines) + "\n")
    return d


@pytest.fixture
def media_root(tmp_path, monkeypatch):
    """注入临时 base_dir，避开真实持久化目录"""
    from app.routers import media as media_router
    from app.routers import traceback as tb_router

    monkeypatch.setattr(
        "app.services.traceback.segment_finder.get_default_base_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(media_router, "get_default_base_dir", lambda: tmp_path)
    monkeypatch.setattr(tb_router, "get_default_base_dir", lambda: tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def _reset_media_token(monkeypatch):
    """每个测试都用确定的 secret，避免随机化干扰"""
    monkeypatch.setattr(
        "app.settings.settings.media_token_secret", "test-stable-secret-2026"
    )
    MediaToken.reset_default()
    yield
    MediaToken.reset_default()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# /traceback/alarm/{id}/evidence
# ---------------------------------------------------------------------------


def _patch_alarm_lookup(monkeypatch, alarm_row):
    """替换 traceback._fetch_alarm 的 DB 实现"""
    from app.routers import traceback as tb_router

    fake_db = MagicMock()
    if alarm_row is None:
        fake_db.query.return_value.filter.return_value.first.return_value = None
    else:
        fake_db.query.return_value.filter.return_value.first.return_value = alarm_row
    fake_db.close = lambda: None
    monkeypatch.setattr(tb_router, "get_db", lambda: iter([fake_db]))


@pytest.mark.asyncio
async def test_evidence_happy_path(client, media_root, monkeypatch):
    # 真实 epoch 时间戳：base + {10s, 20s, 30s, 40s}
    base_us = 1_700_000_000 * 1_000_000
    seg_ts = [
        base_us + 10_000_000,
        base_us + 20_000_000,
        base_us + 30_000_000,
        base_us + 40_000_000,
    ]
    _seed_task(media_root, task_id=100, step_id=1, ts_us_list=seg_ts)

    alarm = SimpleNamespace(
        alarm_id=555, task_id=100, step_id=1, step_name="step",
        alarm_type="bubble", severity="high", message="bubble detected",
        # ms 级，对应 base+22s → trigger 应是 20s 段
        detected_at=(1_700_000_000 + 22) * 1000,
        resolved=False, resolved_by=None, resolved_at=None,
    )
    _patch_alarm_lookup(monkeypatch, alarm)

    resp = await client.get("/traceback/alarm/555/evidence")
    assert resp.status_code == 200, resp.text
    body = resp.json()

    assert body["alarm"]["alarm_id"] == 555
    assert body["task_id"] == 100
    assert body["step_id"] == 1

    # 默认 n_before=1, n_after=2 → 触发段 20s，前 10s，后 30s/40s
    raw_ts = [c["ts_us"] for c in body["raw_clips"]]
    assert raw_ts == seg_ts

    proc_ts = [c["ts_us"] for c in body["processed_clips"]]
    assert proc_ts == seg_ts

    triggers = [c for c in body["processed_clips"] if c["is_trigger"]]
    assert len(triggers) == 1
    assert triggers[0]["ts_us"] == base_us + 20_000_000

    # 每个 URL 含 token
    for clip in body["raw_clips"] + body["processed_clips"]:
        assert "/media/segment/" in clip["url"]

    # keypoints
    assert body["keypoints_url"] is not None
    assert body["detection"] is not None
    assert isinstance(body["detection"], list)


@pytest.mark.asyncio
async def test_evidence_404_when_alarm_missing(client, media_root, monkeypatch):
    _patch_alarm_lookup(monkeypatch, None)

    resp = await client.get("/traceback/alarm/9999/evidence")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evidence_404_when_step_id_missing(client, media_root, monkeypatch):
    """alarm 没有 step_id 时应 404，不再依赖 source_ip 解析。"""
    alarm = SimpleNamespace(
        alarm_id=1, task_id=42, step_id=None, step_name=None,
        alarm_type="x", severity="low", message="m",
        detected_at=1_700_000_000_000,
        resolved=False, resolved_by=None, resolved_at=None,
    )
    _patch_alarm_lookup(monkeypatch, alarm)

    resp = await client.get("/traceback/alarm/1/evidence")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_evidence_handles_seconds_unit(client, media_root, monkeypatch):
    # 段时间戳 1700000010s ~ 1700000040s （以微秒落盘）
    base_us = 1_700_000_010 * 1_000_000
    seg_ts = [base_us, base_us + 10_000_000, base_us + 20_000_000]
    _seed_task(media_root, task_id=7, step_id=2, ts_us_list=seg_ts)

    alarm = SimpleNamespace(
        alarm_id=2, task_id=7, step_id=2, step_name=None,
        alarm_type="x", severity="low", message="m",
        detected_at=1_700_000_022,  # 秒级 (10 位)
        resolved=False, resolved_by=None, resolved_at=None,
    )
    _patch_alarm_lookup(monkeypatch, alarm)

    resp = await client.get("/traceback/alarm/2/evidence?n_before=0&n_after=0")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    triggers = [c for c in body["processed_clips"] if c["is_trigger"]]
    assert len(triggers) == 1
    assert triggers[0]["ts_us"] == base_us + 10_000_000


@pytest.mark.asyncio
async def test_evidence_step_isolation(client, media_root, monkeypatch):
    """同 task 不同 step：alarm.step_id=1 时仅返回 step 1 的段。"""
    base_us = 1_700_000_000 * 1_000_000
    # step 1：早期段；step 2：晚期段（不同 IP / 不同洗消台）
    _seed_task(media_root, task_id=200, step_id=1,
               ts_us_list=[base_us + 10_000_000, base_us + 20_000_000])
    _seed_task(media_root, task_id=200, step_id=2,
               ts_us_list=[base_us + 100_000_000, base_us + 110_000_000])

    alarm = SimpleNamespace(
        alarm_id=8, task_id=200, step_id=1, step_name="leak",
        alarm_type="bubble", severity="high", message="b",
        detected_at=(1_700_000_000 + 12) * 1000,  # 12s 落在 step 1 的 10s 段
        resolved=False, resolved_by=None, resolved_at=None,
    )
    _patch_alarm_lookup(monkeypatch, alarm)

    resp = await client.get("/traceback/alarm/8/evidence?n_before=0&n_after=10")
    assert resp.status_code == 200
    body = resp.json()
    raw_ts = [c["ts_us"] for c in body["raw_clips"]]
    # step 2 的段（100s+）必须被过滤掉
    assert all(ts < base_us + 30_000_000 for ts in raw_ts)
    assert body["step_id"] == 1


# ---------------------------------------------------------------------------
# /traceback/task/{id}/playlist.m3u8
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playlist_vod_generation(client, media_root):
    _seed_task(media_root, task_id=1, step_id=1,
               ts_us_list=[1_000_000, 11_000_000, 21_000_000])

    resp = await client.get("/traceback/task/1/playlist.m3u8?step_id=1&track=processed")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.apple.mpegurl")

    body = resp.text
    assert body.startswith("#EXTM3U")
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in body
    assert "#EXT-X-ENDLIST" in body

    # 应包含 3 条 #EXTINF 和 3 条 token URL
    assert body.count("#EXTINF:") == 3
    assert body.count("/media/segment/") == 3


@pytest.mark.asyncio
async def test_playlist_step_id_required(client, media_root):
    """缺失 step_id query 参数应返回 422。"""
    resp = await client.get("/traceback/task/1/playlist.m3u8")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_playlist_404_when_no_segments(client, media_root):
    resp = await client.get("/traceback/task/999/playlist.m3u8?step_id=1")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_playlist_step_isolation(client, media_root):
    """请求 step=2 时不能返回 step=1 的段。"""
    _seed_task(media_root, task_id=5, step_id=1, ts_us_list=[1_000_000])
    _seed_task(media_root, task_id=5, step_id=2, ts_us_list=[100_000_000, 110_000_000])

    resp = await client.get("/traceback/task/5/playlist.m3u8?step_id=2&track=processed")
    assert resp.status_code == 200
    assert resp.text.count("#EXTINF:") == 2  # 仅 step 2 的两段


@pytest.mark.asyncio
async def test_playlist_raw_track(client, media_root):
    _seed_task(media_root, task_id=2, step_id=1, ts_us_list=[1_000_000, 11_000_000])
    resp = await client.get("/traceback/task/2/playlist.m3u8?step_id=1&track=raw")
    assert resp.status_code == 200
    assert resp.text.count("#EXTINF:") == 2


@pytest.mark.asyncio
async def test_playlist_invalid_track(client, media_root):
    resp = await client.get("/traceback/task/1/playlist.m3u8?step_id=1&track=bogus")
    # FastAPI regex 校验 → 422
    assert resp.status_code == 422


# ---------------------------------------------------------------------------
# /traceback/task/{id}/timeline
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_timeline_returns_alarm_events(client, media_root, monkeypatch):
    _seed_task(media_root, task_id=3, step_id=1,
               ts_us_list=[1_000_000, 11_000_000, 21_000_000])

    from app.routers import traceback as tb_router

    base_ms = 1_700_000_000_000
    rows = [
        SimpleNamespace(
            alarm_id=1, alarm_type="bubble", severity="high",
            message="b", step_id=1, step_name="s1", detected_at=base_ms + 12_000,
        ),
        SimpleNamespace(
            alarm_id=2, alarm_type="bend", severity="med",
            message="bend", step_id=1, step_name="s1", detected_at=base_ms + 2_000,
        ),
    ]
    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = rows
    fake_db.close = lambda: None
    monkeypatch.setattr(tb_router, "get_db", lambda: iter([fake_db]))

    resp = await client.get("/traceback/task/3/timeline?step_id=1")
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["task_id"] == 3
    assert body["step_id"] == 1
    assert body["start_ms"] == 1_000  # 1s
    assert body["end_ms"] == 21_000   # 21s
    assert body["duration_ms"] == 20_000

    assert len(body["events"]) == 2
    # 应按 ts_ms 升序
    assert body["events"][0]["ts_ms"] == base_ms + 2_000
    assert body["events"][0]["alarm_id"] == 2
    assert body["events"][1]["ts_ms"] == base_ms + 12_000


@pytest.mark.asyncio
async def test_timeline_step_id_required(client, media_root):
    resp = await client.get("/traceback/task/999/timeline")
    assert resp.status_code == 422


@pytest.mark.asyncio
async def test_timeline_empty_for_unknown_task(client, media_root, monkeypatch):
    """目录不存在时返回零时长 + 空事件，不再 404。"""
    from app.routers import traceback as tb_router

    fake_db = MagicMock()
    fake_db.query.return_value.filter.return_value.filter.return_value.order_by.return_value.all.return_value = []
    fake_db.close = lambda: None
    monkeypatch.setattr(tb_router, "get_db", lambda: iter([fake_db]))

    resp = await client.get("/traceback/task/999/timeline?step_id=1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["events"] == []
    assert body["duration_ms"] == 0


# ---------------------------------------------------------------------------
# /media/segment/{token} & /media/keypoints/{token}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_segment_with_valid_token(client, media_root):
    d = _seed_task(media_root, task_id=10, step_id=1, ts_us_list=[1_000_000])
    expected = (d / "processed_segment_1000000.mp4").read_bytes()

    token = MediaToken.default().sign(
        task_id=10, step_id=1,
        filename="processed_segment_1000000.mp4", kind="segment",
    )
    resp = await client.get(f"/media/segment/{token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == expected


@pytest.mark.asyncio
async def test_media_segment_invalid_token_rejected(client, media_root):
    resp = await client.get("/media/segment/totally-bogus-token")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_media_segment_wrong_secret_rejected(client, media_root, monkeypatch):
    _seed_task(media_root, task_id=10, step_id=1, ts_us_list=[1_000_000])
    # 用错误的 secret 签发的 token
    attacker = MediaToken(secret=b"attacker-secret", default_ttl=300)
    bad_token = attacker.sign(10, 1, "processed_segment_1000000.mp4", kind="segment")
    resp = await client.get(f"/media/segment/{bad_token}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_media_segment_kind_mismatch_rejected(client, media_root):
    _seed_task(media_root, task_id=10, step_id=1, ts_us_list=[1_000_000])
    # 用 keypoints kind 签发，但访问 segment 路由
    token = MediaToken.default().sign(
        10, 1, "keypoints_1000000.json", kind="keypoints"
    )
    resp = await client.get(f"/media/segment/{token}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_media_segment_missing_file_returns_404(client, media_root):
    # 签发指向不存在文件的 token
    token = MediaToken.default().sign(
        999, 1, "processed_segment_1.mp4", kind="segment"
    )
    resp = await client.get(f"/media/segment/{token}")
    assert resp.status_code == 404


@pytest.mark.asyncio
async def test_media_keypoints_with_valid_token(client, media_root):
    _seed_task(media_root, task_id=10, step_id=1, ts_us_list=[1_000_000])
    token = MediaToken.default().sign(
        10, 1, "keypoints_1000000.json", kind="keypoints"
    )
    resp = await client.get(f"/media/keypoints/{token}")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list)
    assert body[0]["keypoints"] == [1, 2, 3]


@pytest.mark.asyncio
async def test_media_keypoints_wrong_extension_rejected(client, media_root):
    # 签发 keypoints kind，但 filename 是 mp4
    _seed_task(media_root, task_id=10, step_id=1, ts_us_list=[1_000_000])
    token = MediaToken.default().sign(
        10, 1, "processed_segment_1000000.mp4", kind="keypoints"
    )
    resp = await client.get(f"/media/keypoints/{token}")
    assert resp.status_code == 400
