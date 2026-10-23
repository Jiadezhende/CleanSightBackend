"""VisualizationWorker 接渲染子进程池：送渲染、每 run 单飞、收回写回、池拒收时就地渲染。"""

import threading

import numpy as np

from factories import make_cq, make_detector_output, make_frame, make_frame_detection
from app.services.inference.online.render import RenderItem, RenderSpec, RenderType
from app.services.inference.online.topics import LATEST_DETECTION, VIZ_LAST_TS
from app.services.session.topics import LATEST_RAW, PROCESSED, RENDERED
from app.services.inference.online.visualization.visualization_worker import VisualizationWorker


class FakeDetector:
    name = "clean_large"

    def prepare_visualization_data(self, output):
        return RenderSpec(RenderType.BBOX, [RenderItem(bbox=[1, 1, 5, 5], label="hand")], "Large: 1", (0, 255, 0))


class FakeProxy:
    def __init__(self, accept=True):
        self.accept = accept
        self.busy = set()
        self.submitted = []
        self.completions = []

    def in_flight(self, key):
        return key in self.busy

    def submit(self, key, frame, specs, stage, events, ctx):
        if not self.accept:
            return False
        self.submitted.append((key, ctx[2], stage, len(specs)))
        self.busy.add(key)
        return True

    def drain(self):
        out, self.completions = self.completions, []
        for (key, _, _), *_ in out:
            self.busy.discard(key)
        return out


class FakeClients:
    def __init__(self, registry):
        self.registry = registry

    def snapshot(self):
        return dict(self.registry)


def _setup(monkeypatch, proxy, ts=10.0):
    cq = make_cq(task_id=1, stage="2")
    cq.sess.channel(LATEST_RAW).set(make_frame(ts=ts - 0.01, shape=(16, 16, 3)))
    cq.sess.channel(LATEST_DETECTION).set(make_frame_detection(
        ts=ts, by_source={"clean_large": make_detector_output(n=1, ts=ts)},
    ))
    monkeypatch.setattr(
        "app.services.inference.online.visualization.visualization_worker.client_service",
        FakeClients({1: cq}),
    )
    w = VisualizationWorker(
        stop_event=threading.Event(), tick_interval=1 / 30, output_fps=15.0,
        stage_configs={"2": {"models": [FakeDetector()]}}, render_proxy=proxy,
    )
    return w, cq


def test_submits_to_proxy_and_writes_back_on_drain(monkeypatch):
    proxy = FakeProxy()
    w, cq = _setup(monkeypatch, proxy)

    w._tick(100.0)
    assert proxy.submitted == [(1, 10.0, "CLEAN", 1)]
    assert len(cq.sess.channel(PROCESSED)) == 0          # 结果还没回来

    annotated = np.full((16, 16, 3), 77, dtype=np.uint8)
    proxy.completions = [((1, cq, 10.0), annotated, 0.003, True)]
    w._tick(100.1)

    assert [f.timestamp for f in cq.sess.channel(PROCESSED).drain()] == [10.0]
    assert cq.sess.channel(RENDERED).get().frame[0, 0, 0] == 77
    assert w._stat_rendered[1] == 1 and w._render_calls == 1
    assert len(proxy.submitted) == 1                    # 同一推理结果不重送


def test_first_rendered_logged_once_per_run(monkeypatch, caplog):
    import logging

    w, cq = _setup(monkeypatch, FakeProxy(accept=False))
    with caplog.at_level(logging.INFO, logger="app.startup_latency"):
        w._tick(100.0)
        cq.sess.channel(LATEST_DETECTION).set(make_frame_detection(
            ts=11.0, by_source={"clean_large": make_detector_output(n=1, ts=11.0)},
        ))
        w._tick(100.1)

    assert len(cq.sess.channel(PROCESSED)) == 2
    assert sum("first_rendered" in r.getMessage() for r in caplog.records) == 1


def test_dedup_ts_lives_on_session_and_is_released_with_it(monkeypatch):
    """去重时间戳是本 run 的 VIZ_LAST_TS：worker 不再自建 task_id 表，run 关掉即释放。"""
    w, cq = _setup(monkeypatch, FakeProxy(accept=False))
    w._tick(100.0)
    assert cq.sess.channel(VIZ_LAST_TS).get() == 10.0
    assert not hasattr(w, "_last_rendered_ts")
    cq.close()
    assert cq.sess.channel(VIZ_LAST_TS).get() == 0.0


def test_in_flight_run_is_not_resubmitted_or_rendered_locally(monkeypatch):
    proxy = FakeProxy()
    w, cq = _setup(monkeypatch, proxy)
    proxy.busy.add(1)

    w._tick(100.0)

    assert proxy.submitted == [] and len(cq.sess.channel(PROCESSED)) == 0


def test_proxy_rejection_falls_back_to_local_render(monkeypatch):
    w, cq = _setup(monkeypatch, FakeProxy(accept=False))

    w._tick(100.0)

    assert [f.timestamp for f in cq.sess.channel(PROCESSED).drain()] == [10.0]
    assert w._stat_rendered[1] == 1


def test_late_result_for_closed_run_is_rejected_by_cq(monkeypatch):
    proxy = FakeProxy()
    w, cq = _setup(monkeypatch, proxy)
    w._tick(100.0)
    cq.close()
    proxy.completions = [((1, cq, 10.0), np.zeros((16, 16, 3), dtype=np.uint8), 0.003, True)]

    w._tick(100.1)

    assert len(cq.sess.channel(PROCESSED)) == 0
