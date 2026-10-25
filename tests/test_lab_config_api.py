"""`GET` / `PUT /lab/config`：响应带送标上限 `limits`（与 `/submit` 校验同一组 settings）。"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.services.lab import runtime_config
from app.settings import settings


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def _limits(monkeypatch):
    monkeypatch.setattr(settings, "lab_export_max_clips_per_submit", 7)
    monkeypatch.setattr(settings, "lab_export_max_clip_ms", 60_000)
    monkeypatch.setattr(settings, "lab_export_max_total_ms", 120_000)


_LIMITS = {"max_clips": 7, "max_clip_ms": 60_000, "max_total_ms": 120_000}


@pytest.mark.asyncio
async def test_get_config_carries_submit_limits(client, tmp_storage):
    d = (await client.get("/lab/config")).json()
    assert d["limits"] == _LIMITS


@pytest.mark.asyncio
async def test_put_config_carries_submit_limits(client, tmp_storage, monkeypatch):
    monkeypatch.setattr(runtime_config, "update", lambda url, pid, src: {
        "label_studio_url": url, "default_project_id": pid, "task_source": src or "db",
        "token_configured": False, "source": "file",
    })
    d = (await client.put("/lab/config", json={"label_studio_url": "", "default_project_id": 3})).json()
    assert d["limits"] == _LIMITS
    assert d["default_project_id"] == 3
