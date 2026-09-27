"""`/admin-f3m8/offline/jobs`：提交 202 / 未配置 400 / 查询 404 / 列表倒序。

服务换成注入假子进程的实例（同 test_offline_job_service），不起真进程。去重等服务语义
在 test_offline_job_service 里测，路由只是透传。
"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import admin
from app.services.inference.config import InferenceConfig
from app.services.inference.offline.service import OfflineJobService
from doubles import FakeLauncher, offline_result, wait_until

# step 2 / 3 配了离线模型（class 不会被 import，子进程才实例化）；其余 step 一律未配置
_CFG = InferenceConfig({"stages": {
    k: {"offline": {"class": "unused.Segmenter"}} for k in ("2", "3")
}})


@pytest.fixture
def env(monkeypatch, fast_task_queue):
    launcher = FakeLauncher()
    svc = OfflineJobService(config=_CFG, launcher=launcher, poll_s=0.02)
    svc.start()
    monkeypatch.setattr(admin, "offline_job_service", svc)
    yield launcher
    svc.stop(timeout=5.0)


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_submit_then_poll_to_completed(client, env):
    launcher = env
    r = await client.post("/admin-f3m8/offline/jobs", json={"task_id": 1, "step_id": 2})
    assert r.status_code == 202
    assert (r.json()["task_id"], r.json()["step_id"]) == (1, 2)
    assert r.json()["status"] in ("queued", "running")

    wait_until(lambda: len(launcher.procs) == 1)
    launcher.procs[0].finish(0, offline_result(segment_count=4))
    wait_until(lambda: admin.offline_job_service.get(1, 2).status == "completed")

    r = await client.get("/admin-f3m8/offline/jobs/1/2")
    assert r.status_code == 200
    assert (r.json()["status"], r.json()["segment_count"]) == ("completed", 4)


@pytest.mark.asyncio
async def test_unconfigured_step_400(client, env):
    r = await client.post("/admin-f3m8/offline/jobs", json={"task_id": 1, "step_id": 99})
    assert r.status_code == 400
    assert r.json()["field"] == "step_id"


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
