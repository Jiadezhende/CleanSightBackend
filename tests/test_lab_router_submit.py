"""`/lab-f3m8/submit`、`/download`、`/health` 的 HTTP 层：检查顺序、错误体、service 结果到 DTO 的映射。

送标 / 导出 / 探活的业务逻辑在 `services/lab/service.py`（见 test_lab_service.py）；这里把
`lab_service.submit_clips` / `export_step` / `ping_label_studio` 换成替身，不跑 ffmpeg、不连 LS。
"""

from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.lab import runtime_config
from app.services.lab import service as lab_service
from app.services.lab.types import ClipOutcome, SubmitOutcome
from factories import make_run, seed_hls_segments

_TS0_US = 1_700_000_000_000_000


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _configure_ls(monkeypatch, *, url="http://ls", token="tok", default_pid=0):
    monkeypatch.setattr(runtime_config, "get_url", lambda: url)
    monkeypatch.setattr(runtime_config, "get_token", lambda: token)
    monkeypatch.setattr(runtime_config, "get_default_project_id", lambda: default_pid)


def _body(**overrides):
    body = {"task_id": 1, "step_id": 2, "project_id": 7,
            "clips": [{"start_media_ms": 0, "end_media_ms": 1_000}]}
    body.update(overrides)
    return body


# ---------------------------------------------------------------------------
# /submit 检查顺序：503 → 400(project_id) → 400(clips) → 404
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_not_configured_is_503_before_param_checks(client, monkeypatch, tmp_storage):
    _configure_ls(monkeypatch, token="")
    # project_id 缺、区间重叠、无段——全都错，仍先报 503
    r = await client.post("/lab-f3m8/submit", json=_body(project_id=None, clips=[
        {"start_media_ms": 0, "end_media_ms": 1_000}, {"start_media_ms": 500, "end_media_ms": 900},
    ]))
    assert r.status_code == 503
    assert r.json()["detail"] == {
        "error": "Label Studio not configured",
        "detail": "url 可在送标页面「LS 设置」填写；token 须在后端 env 设置 "
        "CLEANSIGHT_LABEL_STUDIO_TOKEN",
    }


@pytest.mark.asyncio
async def test_submit_project_id_checked_before_clips(client, monkeypatch, tmp_storage):
    _configure_ls(monkeypatch)
    r = await client.post("/lab-f3m8/submit", json=_body(project_id=None, clips=[
        {"start_media_ms": 0, "end_media_ms": 1_000}, {"start_media_ms": 500, "end_media_ms": 900},
    ]))
    assert r.status_code == 400
    assert r.json()["field"] == "project_id"


@pytest.mark.asyncio
async def test_submit_clips_checked_before_segments(client, monkeypatch, tmp_storage):
    _configure_ls(monkeypatch)
    r = await client.post("/lab-f3m8/submit", json=_body(clips=[
        {"start_media_ms": 500, "end_media_ms": 900}, {"start_media_ms": 0, "end_media_ms": 1_000},
    ]))
    assert r.status_code == 400
    d = r.json()
    assert d["field"] == "clips"
    assert d["detail"] == "clip[1] overlaps with previous (start_media_ms=500 < prev.end_media_ms=1000)"


@pytest.mark.asyncio
async def test_submit_without_raw_segments_is_404(client, monkeypatch, tmp_storage):
    _configure_ls(monkeypatch, default_pid=7)
    seed_hls_segments(1, 2, [_TS0_US], track="processed")   # 有 run、raw 轨无段
    r = await client.post("/lab-f3m8/submit", json=_body(project_id=None))
    assert r.status_code == 404
    d = r.json()
    assert d["detail"] == "No raw segments for task_id=1, step_id=2"
    assert d["resource_id"] == "task=1,step=2,track=raw"


# ---------------------------------------------------------------------------
# /submit 结果映射
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_submit_maps_outcome_to_response(client, monkeypatch, tmp_storage):
    _configure_ls(monkeypatch)
    seed_hls_segments(1, 2, [_TS0_US])
    calls = []

    def _fake_submit(run, clips, **kw):
        calls.append((run, clips, kw))
        return SubmitOutcome(job_dir=Path("/tmp/job"), clips=[
            ClipOutcome(start_media_ms=0, end_media_ms=1_000, success=True,
                        start_ms=10, end_ms=1_010, label_studio_task_id=42,
                        duration_ms=1_000, size_bytes=3, n_source_segments=1),
            ClipOutcome(start_media_ms=2_000, end_media_ms=3_000, success=False,
                        error_code="range_gap", error="gap"),
        ])

    monkeypatch.setattr(lab_service, "submit_clips", _fake_submit)
    r = await client.post("/lab-f3m8/submit", json=_body(
        clips=[{"start_media_ms": 2_000, "end_media_ms": 3_000},
               {"start_media_ms": 0, "end_media_ms": 1_000}],
        keep_artifacts_on_failure=False,
    ))

    assert r.status_code == 200
    run, clips, kw = calls[0]
    assert run == make_run(1, 2)
    assert [tuple(c) for c in clips] == [(0, 1_000), (2_000, 3_000)]   # 已排序
    assert kw == {"project_id": 7, "ls_url": "http://ls", "ls_token": "tok",
                  "keep_artifacts_on_failure": False}
    d = r.json()
    assert (d["task_id"], d["step_id"], d["run_id"], d["project_id"]) == (1, 2, run.run_id, 7)
    assert d["job_dir"] == str(Path("/tmp/job"))
    assert (d["total"], d["success_count"], d["failure_count"]) == (2, 1, 1)
    assert d["clips"][0] == {
        "start_media_ms": 0, "end_media_ms": 1_000, "start_ms": 10, "end_ms": 1_010,
        "success": True, "label_studio_task_id": 42, "duration_ms": 1_000, "size_bytes": 3,
        "n_source_segments": 1, "error_code": None, "error": None,
    }
    assert d["clips"][1]["error_code"] == "range_gap"


@pytest.mark.asyncio
async def test_submit_job_dir_none_stays_null(client, monkeypatch, tmp_storage):
    _configure_ls(monkeypatch)
    seed_hls_segments(1, 2, [_TS0_US])
    monkeypatch.setattr(lab_service, "submit_clips",
                        lambda run, clips, **kw: SubmitOutcome(job_dir=None, clips=[]))
    r = await client.post("/lab-f3m8/submit", json=_body())
    assert r.status_code == 200
    assert r.json()["job_dir"] is None


# ---------------------------------------------------------------------------
# /download 与 /health
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_download_without_run_is_404_without_exporting(client, monkeypatch, tmp_storage):
    def _boom(*_a, **_kw):
        raise AssertionError("no run → must not export")

    monkeypatch.setattr(lab_service, "export_step", _boom)
    r = await client.get("/lab-f3m8/download", params={"task_id": 1, "step_id": 2, "track": "raw"})
    assert r.status_code == 404
    d = r.json()
    assert d["detail"] == "No raw segments for task_id=1, step_id=2"
    assert d["resource_id"] == "task=1,step=2,track=raw"


@pytest.mark.asyncio
async def test_health_uses_ping(client, monkeypatch, tmp_storage):
    _configure_ls(monkeypatch, default_pid=3)
    monkeypatch.setattr(lab_service, "ping_label_studio",
                        lambda url, token: (False, f"ConnectionError: {url} {token}"))
    r = await client.get("/lab-f3m8/health")
    assert r.status_code == 200
    assert r.json() == {
        "configured": True, "reachable": False, "error": "ConnectionError: http://ls tok",
        "label_studio_url": "http://ls", "default_project_id": 3,
    }
