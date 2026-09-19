"""
追溯路由（/traceback/*, /media/*）端到端测试

覆盖：
- /traceback/task/{id}/playlist.m3u8：必填 step_id，动态 VOD 生成
- /traceback/task/{id}/timeline：必填 step_id，仅返回该 step 的事件
- /media/segment/{token}：合法 token 下载，伪造 token 拒绝
- 路径穿越防御（判据是 `hls.parse_*_name` 解不解得出身份键，不是事后 `relative_to`）

落盘约定：{root}/{task_id}/{step_id}/hls/（`app.storage.hls` 域）
"""

import base64
import hashlib
import hmac
import json
import time
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.traceback.media_token import MediaToken
from app.storage import hls


_SECRET = "test-stable-secret-2026"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _seed_task(task_id: int, step_id: int, ts_us_list, write_init=True):
    """造一个 step 的双轨段 + playlist + init（新布局 `{step}/hls/`）。

    路径一律由 `hls.*_path` / `hls.*_name` 出，不手拼字符串——落盘布局再动一次时
    这里自动跟着走。
    """
    d = hls.init_path(task_id, step_id, "raw").parent
    d.mkdir(parents=True, exist_ok=True)

    for track in hls.TRACKS:
        lines = [
            "#EXTM3U", "#EXT-X-VERSION:7", "#EXT-X-TARGETDURATION:10",
            f'#EXT-X-MAP:URI="{hls.init_name(track)}"',
        ]
        for ts_us in ts_us_list:
            ref = hls.SegmentRef(track=track, ts_us=ts_us)
            hls.segment_path(task_id, step_id, ref).write_bytes(b"\x00" * 16)
            lines.append("#EXTINF:10.000,")
            lines.append(hls.segment_name(ref))
        hls.playlist_path(task_id, step_id, track).write_text("\n".join(lines) + "\n")
        if write_init:
            hls.init_path(task_id, step_id, track).write_bytes(b"\x00" * 8)
    return d


def _forge_token(task_id: int, step_id: int, filename: str, kind: str, ttl: int = 300):
    """绕过 `MediaToken.sign` 的文件名校验直接签一个 token。

    签发侧本来就拦 `/`、`\\`、`..`，所以这种 token 只可能来自伪造；路由必须自己也拦得住
    （纵深防御：签发与消费是两处独立的把关点）。
    """
    payload = {
        "t": task_id, "s": step_id, "f": filename, "k": kind,
        "e": int(time.time()) + ttl,
    }
    payload_bytes = json.dumps(
        payload, separators=(",", ":"), ensure_ascii=False, sort_keys=True
    ).encode("utf-8")
    sig = hmac.new(_SECRET.encode("utf-8"), payload_bytes, hashlib.sha256).digest()

    def _b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")

    return f"{_b64(payload_bytes)}.{_b64(sig)}"


@pytest.fixture
def media_root(tmp_storage):
    """存储根指到隔离临时目录（`tmp_storage` 改的是 settings.storage_dir 单一真源）。"""
    return tmp_storage


@pytest.fixture(autouse=True)
def _reset_media_token(monkeypatch):
    """每个测试都用确定的 secret，避免随机化干扰"""
    monkeypatch.setattr("app.settings.settings.media_token_secret", _SECRET)
    MediaToken.reset_default()
    yield
    MediaToken.reset_default()


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# /traceback/task/{id}/playlist.m3u8
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_playlist_vod_generation(client, media_root):
    _seed_task(task_id=1, step_id=1,
               ts_us_list=[1_000_000, 11_000_000, 21_000_000])

    resp = await client.get("/traceback/task/1/playlist.m3u8?step_id=1&track=processed")
    assert resp.status_code == 200, resp.text
    assert resp.headers["content-type"].startswith("application/vnd.apple.mpegurl")

    body = resp.text
    assert body.startswith("#EXTM3U")
    assert "#EXT-X-VERSION:7" in body
    assert "#EXT-X-PLAYLIST-TYPE:VOD" in body
    assert "#EXT-X-ENDLIST" in body

    # fMP4 必备：EXT-X-MAP 指向 /media/init/{token}
    assert "#EXT-X-MAP:URI=" in body
    assert "/media/init/" in body

    # 应包含 3 条 #EXTINF 和 3 条 segment token URL
    assert body.count("#EXTINF:") == 3
    assert body.count("/media/segment/") == 3


@pytest.mark.asyncio
async def test_playlist_503_when_init_missing(client, media_root):
    """缺 `{track}_init.mp4` 时 playlist 端点应 503——fMP4 无 init 段无法解码，
    且服务端无法自愈（旧格式产物不支持迁移，或首段仍在 transcode）。"""
    _seed_task(task_id=42, step_id=1,
               ts_us_list=[1_000_000], write_init=False)

    resp = await client.get("/traceback/task/42/playlist.m3u8?step_id=1&track=raw")
    assert resp.status_code == 503
    detail = resp.json().get("detail", {})
    assert isinstance(detail, dict)
    assert "init" in detail.get("error", "").lower()
    # 只断言指认了缺失的具体文件，不绑定整句措辞
    assert "raw_init.mp4" in detail.get("detail", "")


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
    _seed_task(task_id=5, step_id=1, ts_us_list=[1_000_000])
    _seed_task(task_id=5, step_id=2, ts_us_list=[100_000_000, 110_000_000])

    resp = await client.get("/traceback/task/5/playlist.m3u8?step_id=2&track=processed")
    assert resp.status_code == 200
    assert resp.text.count("#EXTINF:") == 2  # 仅 step 2 的两段


@pytest.mark.asyncio
async def test_playlist_raw_track(client, media_root):
    _seed_task(task_id=2, step_id=1, ts_us_list=[1_000_000, 11_000_000])
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
    _seed_task(task_id=3, step_id=1,
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
    assert body["start_ms"] == 1_000  # 1s（首段起点 ts_us=1_000_000）
    # end_ms = 末段起点 + EXTINF。_seed_task 给每段 EXTINF=10s，末段 ts=21s → end=31s。
    # 不能用「最大 ts_us」当 end，否则漏算最后一段自身长度，跟 hls.js 实播总时长对不上。
    assert body["end_ms"] == 31_000   # 21s + 10s EXTINF
    assert body["duration_ms"] == 30_000

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
# /media/segment/{token}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_segment_with_valid_token(client, media_root):
    d = _seed_task(task_id=10, step_id=1, ts_us_list=[1_000_000])
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
    _seed_task(task_id=10, step_id=1, ts_us_list=[1_000_000])
    # 用错误的 secret 签发的 token
    attacker = MediaToken(secret=b"attacker-secret", default_ttl=300)
    bad_token = attacker.sign(10, 1, "processed_segment_1000000.mp4", kind="segment")
    resp = await client.get(f"/media/segment/{bad_token}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_media_segment_kind_mismatch_rejected(client, media_root):
    _seed_task(task_id=10, step_id=1, ts_us_list=[1_000_000])
    # 用 init kind 签发，但访问 segment 路由
    token = MediaToken.default().sign(
        10, 1, "init.mp4", kind="init"
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
@pytest.mark.parametrize(
    "filename",
    ["../../etc/passwd", "..\\..\\windows\\win.ini", "..", "raw_playlist.m3u8"],
)
async def test_media_segment_rejects_non_segment_names(client, media_root, filename):
    """段名解不出 `SegmentRef` → 400，**不是 404**。

    路径由 `hls.segment_path(ref)` 按结构重建，外部字符串根本不进入拼接，所以穿越串
    连"文件不存在"都到不了——它在解析那一步就被判死。400 而非 404 也更诚实：请求本身
    非法，不是资源缺失。
    """
    token = _forge_token(10, 1, filename, kind="segment")
    resp = await client.get(f"/media/segment/{token}")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# /media/init/{token}
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_media_init_with_valid_token(client, media_root):
    d = _seed_task(task_id=10, step_id=1, ts_us_list=[1_000_000])
    expected = (d / "raw_init.mp4").read_bytes()

    token = MediaToken.default().sign(
        task_id=10, step_id=1, filename="raw_init.mp4", kind="init",
    )
    resp = await client.get(f"/media/init/{token}")
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "video/mp4"
    assert resp.content == expected


@pytest.mark.asyncio
async def test_media_init_kind_mismatch_rejected(client, media_root):
    """segment kind 的 token 不能从 /media/init 拿数据。"""
    _seed_task(task_id=10, step_id=1, ts_us_list=[1_000_000])
    token = MediaToken.default().sign(
        10, 1, "init.mp4", kind="segment",
    )
    resp = await client.get(f"/media/init/{token}")
    assert resp.status_code == 403


@pytest.mark.asyncio
async def test_media_init_wrong_filename_rejected(client, media_root):
    """init kind 的 token 必须指向 init.mp4，不能借此读其它 mp4 段。"""
    _seed_task(task_id=10, step_id=1, ts_us_list=[1_000_000])
    token = MediaToken.default().sign(
        10, 1, "processed_segment_1000000.mp4", kind="init",
    )
    resp = await client.get(f"/media/init/{token}")
    assert resp.status_code == 400


@pytest.mark.asyncio
@pytest.mark.parametrize("filename", ["evil_init.mp4", "init.mp4", "_init.mp4"])
async def test_media_init_rejects_lookalike_init_names(client, media_root, filename):
    """`evil_init.mp4` 必须 400 —— 这是 `endswith("init.mp4")` 放行、
    `hls.parse_init_name` 拦下的那一类名字（判据是完整匹配 `{raw|processed}_init.mp4`）。

    裸 `init.mp4` 同样不合法：两轨各有各的 init，不带 track 前缀的名字指不出任何一份。
    """
    _seed_task(task_id=11, step_id=1, ts_us_list=[1_000_000])
    (media_root / "11" / "1" / "hls" / "evil_init.mp4").write_bytes(b"pwned")

    token = MediaToken.default().sign(11, 1, filename, kind="init")
    resp = await client.get(f"/media/init/{token}")
    assert resp.status_code == 400


@pytest.mark.asyncio
async def test_media_init_missing_file_returns_404(client, media_root):
    """名字合法但盘上没有 → 404（区别于名字非法的 400）。"""
    _seed_task(
        task_id=11, step_id=1,
        ts_us_list=[1_000_000], write_init=False,
    )
    token = MediaToken.default().sign(
        11, 1, "raw_init.mp4", kind="init",
    )
    resp = await client.get(f"/media/init/{token}")
    assert resp.status_code == 404
