"""Daemon lifecycle uses real ephemeral listeners and simulated child processes."""

import asyncio
from dataclasses import replace
import importlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.daemons.mediamtx.config import MediaMTXConfig, get_mediamtx_config, resolve_binary, ROOT
from app.daemons.mediamtx.worker import MediaMTXWorker
from app.daemons.mediamtx import worker as worker_module
from app.gateway import RateLimitStore
from app.settings import Settings


@pytest.fixture
def config(tmp_path):
    binary = tmp_path / "mediamtx"
    binary.write_text("fixture", encoding="utf-8")
    source = tmp_path / "config.yml"
    source.write_text("rtsp: true", encoding="utf-8")
    return MediaMTXConfig(str(binary), str(source), 0, 18004, 8002, 8003,
                          True, frozenset(), 30, 60, 5, 60, 3600)


class Child:
    def __init__(self, stubborn=False):
        self.returncode = None
        self.stdout = asyncio.StreamReader()
        self.stdout.feed_eof()
        self.done = asyncio.Event()
        self.stubborn = stubborn
        self.terminated = False
        self.killed = False

    async def wait(self):
        await self.done.wait()
        return self.returncode

    def finish(self, code):
        self.returncode = code
        self.done.set()

    def terminate(self):
        self.terminated = True
        if not self.stubborn:
            self.finish(0)

    def kill(self):
        self.killed = True
        self.finish(-9)


async def wait_state(worker, state):
    async def wait():
        while worker.get_status()["state"] != state:
            await asyncio.sleep(0)
    await asyncio.wait_for(wait(), timeout=2)


def test_configuration_shares_policy_but_not_protocol_limit(monkeypatch):
    settings_module = importlib.import_module("app.settings")
    settings = Settings(gateway_allowed_ips="127.0.0.1", gateway_rate_limit=60,
                        rtsp_rate_limit=7, gateway_rate_window=12,
                        gateway_rate_ban_threshold=3, gateway_rate_ban_window=22,
                        gateway_ban_duration=33, gateway_enabled=False)
    monkeypatch.setattr(settings_module, "settings", settings)
    monkeypatch.setenv("GATEWAY_LISTEN_PORT", "12345")
    result = get_mediamtx_config()
    assert result.listen_port == settings.mediamtx_proxy_port
    assert result.allowed_ips == settings.allowed_ips_set
    assert (result.rate_limit, result.rate_window, result.ban_threshold,
            result.ban_window, result.ban_duration) == (7, 12, 3, 22, 33)
    assert result.access_checks is False
    assert settings.gateway_rate_limit == 60
    name = "mediamtx.exe" if os.name == "nt" else "mediamtx"
    assert resolve_binary("auto") == str(ROOT / ".mediamtx" / name)
    assert resolve_binary("") == ""
    assert resolve_binary("custom/bin") == str(ROOT / "custom/bin")
    absolute = str(ROOT / "explicit/bin")
    assert resolve_binary(absolute) == absolute


@pytest.mark.asyncio
async def test_constructor_and_pure_proxy_repeated_start_stop(config, monkeypatch):
    spawn = AsyncMock()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    daemon = MediaMTXWorker(replace(config, binary=""))
    assert daemon._ratelimit is None and daemon._proxy is None
    await daemon.start()
    proxy = daemon._proxy
    store = daemon._ratelimit
    await daemon.start()
    assert daemon._proxy is proxy
    assert daemon.get_status()["managed"] is False
    await daemon.stop()
    await daemon.stop()
    assert not store._cleanup_thread.is_alive()
    spawn.assert_not_called()
    await daemon.start()
    assert daemon.get_status()["state"] == "running"
    await daemon.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "spawn", "ready", "bind"])
async def test_start_failure_rolls_back_resources(config, monkeypatch, failure):
    daemon = MediaMTXWorker(config)
    child = Child()
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(daemon, "_wait_ready", AsyncMock())
    if failure == "missing":
        Path(config.binary).unlink()
    elif failure == "spawn":
        spawn.side_effect = OSError("fixture")
    elif failure == "ready":
        monkeypatch.setattr(daemon, "_wait_ready", AsyncMock(side_effect=asyncio.TimeoutError))
    else:
        monkeypatch.setattr(worker_module.RTSPProxy, "start", AsyncMock(side_effect=OSError("busy")))
    with pytest.raises((OSError, asyncio.TimeoutError)):
        await daemon.start()
    assert daemon.get_status()["state"] == "failed"
    assert daemon._proxy is None and daemon._ratelimit is None and daemon._process is None
    if failure == "ready":
        assert child.terminated
    await daemon.stop()


@pytest.mark.asyncio
async def test_cancellation_during_start_reaps_child(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    child = Child()
    entered = asyncio.Event()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))

    async def blocked_ready():
        entered.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(daemon, "_wait_ready", blocked_ready)
    task = asyncio.create_task(daemon.start())
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert child.terminated and daemon._proxy is None and daemon._process is None


@pytest.mark.asyncio
async def test_managed_child_environment_and_forced_stop(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    child = Child(stubborn=True)
    spawn = AsyncMock(return_value=child)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(daemon, "_wait_ready", AsyncMock())
    monkeypatch.setattr(worker_module, "_STOP_TIMEOUT", 0.01)
    monkeypatch.setenv("MTX_RTSPADDRESS", "parent-value")
    await daemon.start()
    environment = spawn.call_args.kwargs["env"]
    assert environment["MTX_RTSPADDRESS"] == "127.0.0.1:18004"
    assert environment["MTX_RTPADDRESS"] == "127.0.0.1:8002"
    assert environment["MTX_RTCPADDRESS"] == "127.0.0.1:8003"
    assert os.environ["MTX_RTSPADDRESS"] == "parent-value"
    store = daemon._ratelimit
    await daemon.stop()
    assert child.terminated and child.killed
    assert daemon._supervisor is None and daemon._process is None
    assert not store._cleanup_thread.is_alive()


@pytest.mark.asyncio
async def test_crash_restarts_then_exhausts(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    children = []

    async def spawn(*args, **kwargs):
        child = Child()
        children.append(child)
        asyncio.get_running_loop().call_soon(child.finish, 1)
        return child

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(daemon, "_wait_ready", AsyncMock())
    monkeypatch.setattr(daemon, "_backoff", AsyncMock(return_value=True))
    await daemon.start()
    await wait_state(daemon, "failed")
    await daemon._supervisor
    assert len(children) == 6
    assert daemon.get_status()["restart_count"] == 5
    assert daemon.get_status()["error_code"] == "retries_exhausted"
    assert daemon._proxy is None and daemon._ratelimit is None
    await daemon.stop()


@pytest.mark.asyncio
async def test_unexpected_clean_exit_fails_without_restart(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    child = Child()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    monkeypatch.setattr(daemon, "_wait_ready", AsyncMock())
    await daemon.start()
    child.finish(0)
    await wait_state(daemon, "failed")
    await daemon._supervisor
    assert daemon.get_status()["error_code"] == "unexpected_exit"
    assert daemon.get_status()["restart_count"] == 0
    await daemon.stop()


@pytest.mark.asyncio
async def test_stop_interrupts_backoff(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    child = Child()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    monkeypatch.setattr(daemon, "_wait_ready", AsyncMock())
    await daemon.start()
    child.finish(1)
    await wait_state(daemon, "restarting")
    await asyncio.wait_for(daemon.stop(), timeout=1)
    assert daemon.get_status()["state"] == "stopped"


@pytest.mark.asyncio
async def test_supervisor_cancellation_reaps_resources(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    child = Child()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    monkeypatch.setattr(daemon, "_wait_ready", AsyncMock())
    await daemon.start()
    await asyncio.sleep(0)
    daemon._supervisor.cancel()
    await daemon._supervisor
    assert daemon.get_status()["state"] == "failed"
    assert child.terminated and daemon._proxy is None
    await daemon.stop()


@pytest.mark.asyncio
async def test_stop_reaps_supervisor_cancelled_before_first_turn(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    child = Child()
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=child))
    monkeypatch.setattr(daemon, "_wait_ready", AsyncMock())
    await daemon.start()
    daemon._supervisor.cancel()
    await daemon.stop()
    assert child.terminated and daemon._proxy is None
    assert daemon.get_status()["state"] == "stopped"


@pytest.mark.asyncio
async def test_lifespan_cleans_up_after_body_failure(monkeypatch):
    from app.daemons import mediamtx
    from app.daemons.mediamtx import instance

    fake = SimpleNamespace(start=AsyncMock(), stop=AsyncMock())
    monkeypatch.setattr(instance, "mediamtx_worker", fake)
    with pytest.raises(RuntimeError, match="fixture"):
        async with mediamtx.lifespan():
            fake.start.assert_awaited_once()
            raise RuntimeError("fixture")
    fake.stop.assert_awaited_once()


@pytest.mark.asyncio
async def test_ready_probe_is_bounded_and_checks_child(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    daemon._config = config
    daemon._process = Child()
    monkeypatch.setattr(worker_module, "_START_TIMEOUT", 0.01)
    monkeypatch.setattr(asyncio, "open_connection", AsyncMock(side_effect=ConnectionRefusedError))
    with pytest.raises(asyncio.TimeoutError):
        await daemon._wait_ready()
    daemon._process.finish(1)
    with pytest.raises(RuntimeError, match="exited"):
        await daemon._wait_ready()


@pytest.mark.asyncio
async def test_ready_probe_connects_to_configured_port(config):
    async def accept(reader, writer):
        writer.close()
        await writer.wait_closed()

    server = await asyncio.start_server(accept, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    daemon = MediaMTXWorker(config)
    daemon._config = replace(config, target_port=port)
    daemon._process = Child()
    try:
        await daemon._wait_ready()
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_failed_restart_reaps_new_child(config, monkeypatch):
    daemon = MediaMTXWorker(config)
    first, second = Child(), Child()
    spawn = AsyncMock(side_effect=[first, second])
    monkeypatch.setattr(asyncio, "create_subprocess_exec", spawn)
    monkeypatch.setattr(daemon, "_wait_ready", AsyncMock(side_effect=[None, OSError("not ready")]))
    monkeypatch.setattr(daemon, "_backoff", AsyncMock(return_value=True))
    await daemon.start()
    first.finish(1)
    await wait_state(daemon, "failed")
    await daemon._supervisor
    assert second.terminated and daemon._proxy is None
    assert daemon.get_status()["error_code"] == "supervision_failed"
    await daemon.stop()


@pytest.mark.asyncio
async def test_rtsp_policy_bans_and_checks_can_be_disabled(config):
    first = MediaMTXWorker(replace(config, binary="", rate_limit=1, ban_threshold=2))
    second = MediaMTXWorker(replace(config, binary="", access_checks=False))
    await first.start()
    await second.start()
    try:
        store = first._ratelimit
        whitelist = first._proxy._whitelist
        assert store.is_allowed("127.0.0.1")
        assert not store.is_allowed("127.0.0.1")
        assert whitelist.is_allowed("127.0.0.1")
        assert not store.is_allowed("127.0.0.1")
        assert not whitelist.is_allowed("127.0.0.1")
        assert second._proxy._whitelist.is_allowed("127.0.0.1")
        assert second._ratelimit.is_allowed("127.0.0.1")
        assert second._proxy._access_checks is False
    finally:
        await first.stop()
        await second.stop()


def test_health_reports_failure_and_other_routes_remain_available(monkeypatch):
    from app.routers import health
    from app.daemons.mediamtx import instance

    state = {"state": "running", "managed": True, "restart_count": 0, "error_code": None}
    monkeypatch.setattr(instance, "mediamtx_worker", SimpleNamespace(get_status=lambda: dict(state)))
    monkeypatch.setattr(health, "health_monitor_worker", SimpleNamespace(
        is_running=True, get_system_status=lambda: {"clients": {"total": 0}},
    ))
    app = FastAPI()
    app.include_router(health.router)
    app.get("/other")(lambda: {"ok": True})
    client = TestClient(app)
    assert client.get("/health/status").status_code == 200
    state.update(state="restarting", restart_count=1)
    assert client.get("/health/status").json()["mediamtx"]["state"] == "restarting"
    state.update(state="failed", error_code="retries_exhausted")
    response = client.get("/health/status")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["clients"] == {"total": 0}
    assert client.get("/other").status_code == 200
    monkeypatch.setattr(health.health_monitor_worker, "is_running", False)
    assert client.get("/health/status").status_code == 503


def test_rate_store_close_is_idempotent():
    store = RateLimitStore(1, 60)
    store.close()
    store.close()
    assert not store._cleanup_thread.is_alive()
