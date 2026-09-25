"""`/admin-f3m8/offline/jobs`：提交 202 / 去重 / live 409 / 查询 404。

服务换成注入假子进程与假注册表的实例（同 test_offline_job_service），不起真进程。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import admin
from app.services.inference.offline.service import OfflineJobService
from test_offline_job_service import FakeClients, FakeLauncher, _ok, _wait_until


@pytest.fixture
def env(monkeypatch):
    clients, launcher = FakeClients(), FakeLauncher()
    svc = OfflineJobService(clients=clients, launcher=launcher, poll_s=0.02)
    svc.start()
    monkeypatch.setattr(admin, "offline_job_service", svc)
    yield clients, launcher
    svc.stop(timeout=5.0)


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_submit_then_poll_to_completed(client, env):
    _, launcher = env
    r = await client.post("/admin-f3m8/offline/jobs", json={"task_id": 1, "step_id": 2})
    assert r.status_code == 202
    assert (r.json()["task_id"], r.json()["step_id"]) == (1, 2)
    assert r.json()["status"] in ("queued", "running")

    _wait_until(lambda: len(launcher.procs) == 1)
    launcher.procs[0].finish(0, _ok(segment_count=4))
    _wait_until(lambda: admin.offline_job_service.get(1, 2).status == "completed")

    r = await client.get("/admin-f3m8/offline/jobs/1/2")
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["segment_count"]) == ("completed", 4)


@pytest.mark.asyncio
async def test_duplicate_submit_returns_same_job(client, env):
    a = (await client.post("/admin-f3m8/offline/jobs", json={"task_id": 1, "step_id": 2})).json()
    b = (await client.post("/admin-f3m8/offline/jobs", json={"task_id": 1, "step_id": 2})).json()
    assert a["submitted_at"] == b["submitted_at"]


@pytest.mark.asyncio
async def test_live_step_409(client, env):
    clients, _ = env
    clients.go_live(1, 2)
    r = await client.post("/admin-f3m8/offline/jobs", json={"task_id": 1, "step_id": 2})
    assert r.status_code == 409


@pytest.mark.asyncio
async def test_unknown_job_404(client, env):
    r = await client.get("/admin-f3m8/offline/jobs/9/9")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_list_newest_first(client, env):
    await client.post("/admin-f3m8/offline/jobs", json={"task_id": 1, "step_id": 2})
    await client.post("/admin-f3m8/offline/jobs", json={"task_id": 1, "step_id": 3})
    r = await client.get("/admin-f3m8/offline/jobs")
    assert [j["step_id"] for j in r.json()["jobs"]] == [3, 2]
