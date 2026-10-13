"""内存兜底：ca_ready 独立浅上限、最坏占用估算、启动自检。"""

import logging
from types import SimpleNamespace

import pytest

import factories
from app import main as app_main
from app.services.client.config import ClientConfig, FrameConfig, StateConfig
from app.services.utils import memory_budget
from app.services.utils.memory_budget import MemoryPlan, estimate, read_mem_available

MB = 2**20
GIB = 2**30
FRAME = 640 * 480 * 3


# ---------------------------------------------------------------------------
# ca_ready 独立上限
# ---------------------------------------------------------------------------


def test_client_config_derives_shallow_ready_cap():
    cfg = ClientConfig(frame=FrameConfig(), state=StateConfig())
    assert cfg.ca_maxlen == 20 * 30          # ca_maxlen_seconds=20 × raw_fps
    assert cfg.ca_ready_maxlen == 30         # 2s × inference_fps(15)
    assert cfg.cq_kwargs()["ca_ready_maxlen"] == 30


def test_ready_cap_is_independent_of_raw_cap():
    cq = factories.make_cq(ca_maxlen=10, ca_ready_maxlen=2, inference_decimation=1)
    for i in range(5):
        f = factories.make_frame(ts=float(i))
        cq.append_ca_raw(f)
        cq.append_ca_ready_with_throttle(f)
    assert len(cq.ca_ready) == 2
    assert cq.frames_dropped_ready == 3
    assert len(cq.ca_raw) == 5


def test_ready_cap_defaults_to_ca_maxlen_for_bare_cq():
    cq = factories.make_bare_cq(ca_maxlen=7)
    assert cq.ca_ready.maxlen == 7


# ---------------------------------------------------------------------------
# 最坏占用估算
# ---------------------------------------------------------------------------


def _plan(**over):
    base = dict(streams=15, frame_bytes=FRAME, ca_maxlen=600, ca_ready_maxlen=30, segment_len=300,
                recording_budget_bytes=8192 * MB, baseline_bytes=6144 * MB)
    base.update(over)
    return MemoryPlan(**base)


def test_estimate_scenarios_take_max_not_sum():
    est = estimate(_plan())
    normal = 15 * 2 * 300 * FRAME
    ready = 15 * 30 * FRAME
    assert est.normal == normal
    assert est.recording_stall == normal + 8192 * MB
    assert est.inference_stall == normal + ready
    assert est.sweeper_stall == 15 * 2 * 600 * FRAME + ready
    assert est.worst == max(est.recording_stall, est.inference_stall, est.sweeper_stall) + 6144 * MB


def test_default_plan_fits_32g_server_but_30s_cache_does_not():
    """提案的定参依据：32GB 机器 MemAvailable ≈ 29GiB，20s 缓存过、30s 缓存不过。"""
    available = 29 * GIB
    assert estimate(_plan()).worst <= available
    assert estimate(_plan(ca_maxlen=900)).worst > available


def test_read_mem_available(tmp_path):
    p = tmp_path / "meminfo"
    p.write_text("MemTotal:       32000000 kB\nMemAvailable:   29000000 kB\n", encoding="ascii")
    assert read_mem_available(str(p)) == 29000000 * 1024
    assert read_mem_available(str(tmp_path / "missing")) is None


# ---------------------------------------------------------------------------
# 启动自检
# ---------------------------------------------------------------------------


def _settings(**over):
    base = dict(capacity_planned_streams=15, memory_baseline_mb=6144, memory_check_enforce=True)
    base.update(over)
    return SimpleNamespace(**base)


def test_startup_check_passes_with_enough_memory(monkeypatch):
    monkeypatch.setattr(memory_budget, "read_mem_available", lambda: 64 * GIB)
    app_main._check_memory_budget(_settings())


def test_startup_check_refuses_when_over_budget(monkeypatch):
    monkeypatch.setattr(memory_budget, "read_mem_available", lambda: 8 * GIB)
    with pytest.raises(RuntimeError, match="内存自检未通过"):
        app_main._check_memory_budget(_settings())


def test_startup_check_only_warns_when_not_enforced(monkeypatch, caplog):
    monkeypatch.setattr(memory_budget, "read_mem_available", lambda: 8 * GIB)
    with caplog.at_level(logging.WARNING):
        app_main._check_memory_budget(_settings(memory_check_enforce=False))
    assert "内存自检未通过" in caplog.text


def test_startup_check_skips_without_meminfo(monkeypatch, caplog):
    monkeypatch.setattr(memory_budget, "read_mem_available", lambda: None)
    with caplog.at_level(logging.WARNING):
        app_main._check_memory_budget(_settings())
    assert "跳过" in caplog.text
