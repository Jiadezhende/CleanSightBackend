"""`GET /admin/stages`：推理配置 → step 能力表（别名 / 离线模型 / 离线规则判定）。"""

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.main import app
from app.routers import admin as admin_router
from app.services.inference.config import InferenceConfig


@pytest_asyncio.fixture
async def client():
    transport = ASGITransport(app=app, client=("127.0.0.1", 9999))
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _use_config(monkeypatch, stages):
    config = InferenceConfig({"stages": stages})
    monkeypatch.setattr(admin_router, "load_stage_config", lambda: config)


@pytest.mark.asyncio
async def test_stages_from_config_sorted_by_step_id(client, monkeypatch):
    _use_config(monkeypatch, {
        "10": {"alias": "LATE"},
        "2": {"alias": "CLEAN", "offline": {"class": "x.Seg", "params": {}},
              "compliance": {"rules": [{"id": "r1"}]}},
        "1": {"alias": "LEAK", "offline": {}},
    })

    d = (await client.get("/admin/stages")).json()

    assert d == {"stages": [
        {"step_id": 1, "alias": "LEAK", "has_offline_model": False, "has_compliance": False},
        {"step_id": 2, "alias": "CLEAN", "has_offline_model": True, "has_compliance": True},
        {"step_id": 10, "alias": "LATE", "has_offline_model": False, "has_compliance": False},
    ]}


@pytest.mark.asyncio
async def test_alias_falls_back_to_key_and_non_integer_keys_skipped(client, monkeypatch):
    _use_config(monkeypatch, {
        "3": {"compliance": {"evidence": {}, "rules": []}},   # rules 为空 = 不核对
        "debug": {"alias": "DBG"},
    })

    d = (await client.get("/admin/stages")).json()

    assert d == {"stages": [
        {"step_id": 3, "alias": "3", "has_offline_model": False, "has_compliance": False},
    ]}


@pytest.mark.asyncio
async def test_real_config_lists_known_steps(client):
    """仓库里的 config/inference_config.yaml 至少声明了 step 1 / 2，字段齐全。"""
    d = (await client.get("/admin/stages")).json()
    by_id = {s["step_id"]: s for s in d["stages"]}
    assert {1, 2} <= set(by_id)
    assert all(set(s) == {"step_id", "alias", "has_offline_model", "has_compliance"} for s in d["stages"])
