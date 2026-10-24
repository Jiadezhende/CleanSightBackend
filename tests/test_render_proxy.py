"""RemoteRenderProxy / render_child：每键单飞、槽位归还、失败清理、子进程请求循环（不 spawn 真子进程）。"""

import queue

import numpy as np
import pytest

from app.services.inference.online.render import RenderItem, RenderSpec, RenderType
from app.services.inference.online.visualization import render_child
from app.services.inference.online.visualization.render_proxy import RemoteRenderProxy
from app.services.utils.frame_ring import SharedFrameRing, SharedFrameRingView
from app.services.utils.metrics import frame_drop_total

H, W = 48, 64
SLOT = H * W * 3


def _frame(v=0):
    return np.full((H, W, 3), v, dtype=np.uint8)


def _specs():
    return [RenderSpec(
        type=RenderType.BBOX,
        items=[RenderItem(bbox=[4, 4, 30, 30], label="hand 0.9", color=(0, 255, 0))],
        status_text="Large: 1", status_color=(0, 255, 0),
    )]


@pytest.fixture
def proxy():
    """不 spawn 的 proxy：真环 + 进程内队列 + 置就绪。"""
    p = RemoteRenderProxy(processes=1, slot_bytes=SLOT, slots=3)
    p._ring = SharedFrameRing(slots=3, slot_bytes=SLOT, prefix="cstest")
    p._req_q = queue.Queue()
    p._resp_q = queue.Queue()
    p._ready.set()
    yield p
    p._ring.close()


def _answer(p, *, ok=True, fill=None):
    """冒充子进程：取一条请求，把槽位改写成 fill（模拟渲染结果），回答复。"""
    req_id, slot, shape, *_ = p._req_q.get_nowait()
    if fill is not None:
        view = SharedFrameRingView(p._ring.spec)
        view.write(slot, np.full(shape, fill, dtype=np.uint8))
        view.close()
    p._resp_q.put((req_id, shape, 0.004, ok))


def _drop(reason):
    return frame_drop_total.labels(reason=reason)._value.get()


def test_single_flight_per_key(proxy):
    assert proxy.submit(1, _frame(), _specs(), "CLEAN", [], ctx="a") is True
    assert proxy.in_flight(1)
    assert proxy.submit(1, _frame(), _specs(), "CLEAN", [], ctx="b") is False   # 同键在途
    assert proxy.submit(2, _frame(), _specs(), "CLEAN", [], ctx="c") is True    # 异键不受影响


def test_drain_returns_result_from_slot_and_releases(proxy):
    proxy.submit(1, _frame(0), _specs(), "CLEAN", ["Action: flush"], ctx=("t1", 1.0))
    _answer(proxy, fill=200)

    (ctx, annotated, render_s, ok), = proxy.drain()
    assert ctx == ("t1", 1.0) and ok and render_s == 0.004
    assert annotated.shape == (H, W, 3) and annotated[0, 0, 0] == 200
    assert not proxy.in_flight(1)
    assert proxy._ring.free_count == 3


def test_ring_full_and_oversize_rejected(proxy):
    for k in range(3):
        assert proxy.submit(k, _frame(), _specs(), "CLEAN", [], ctx=k) is True
    assert proxy.submit(9, _frame(), _specs(), "CLEAN", [], ctx=9) is False         # 槽满
    big = np.zeros((H * 2, W, 3), dtype=np.uint8)
    assert proxy.submit(10, big, _specs(), "CLEAN", [], ctx=10) is False            # 放不进


def test_not_ready_rejects(proxy):
    proxy._ready.clear()
    assert proxy.submit(1, _frame(), _specs(), "CLEAN", [], ctx=1) is False


def test_put_failure_releases_slot(proxy):
    class FullQ:
        def put(self, *a, **k):
            raise queue.Full()
    proxy._req_q = FullQ()
    assert proxy.submit(1, _frame(), _specs(), "CLEAN", [], ctx=1) is False
    assert not proxy.in_flight(1) and proxy._ring.free_count == 3


def test_orphan_response_ignored(proxy):
    proxy._resp_q.put((999, (H, W, 3), 0.001, True))
    assert proxy.drain() == []


def test_failure_clears_inflight_and_counts_drops(proxy):
    proxy._stop_event.set()           # 只验清理，不真重启
    proxy.submit(1, _frame(), _specs(), "CLEAN", [], ctx=1)
    proxy.submit(2, _frame(), _specs(), "CLEAN", [], ctx=2)
    before = _drop("viz_child_down")

    proxy._handle_failure(dead=True, wedged=False, not_ready=False)

    assert not proxy.in_flight(1) and proxy._ring.free_count == 3
    assert _drop("viz_child_down") - before == 2


# ---------------------------------------------------------------------------
# 子进程请求循环（在本线程直接驱动 serve）
# ---------------------------------------------------------------------------


def _serve_once(ring, item, vis):
    req_q, resp_q = queue.Queue(), queue.Queue()
    req_q.put(item)
    req_q.put(None)
    view = SharedFrameRingView(ring.spec)
    try:
        render_child.serve(req_q, resp_q, view, vis)
    finally:
        view.close()
    return resp_q.get_nowait()


def test_serve_renders_into_same_slot():
    from app.services.inference.online.visualization.visualizer import FixedVisualizer

    ring = SharedFrameRing(slots=1, slot_bytes=SLOT, prefix="cstest")
    try:
        (slot,) = ring.acquire(1)
        ring.write(slot, _frame(0))
        req_id, shape, render_s, ok = _serve_once(
            ring, (7, slot, (H, W, 3), _specs(), "CLEAN", ["Action: flush"]), FixedVisualizer(),
        )
        assert (req_id, shape, ok) == (7, (H, W, 3), True)
        assert ring.read(slot, shape).any()          # 画了框和字，不再全黑
    finally:
        ring.close()


def test_serve_render_error_keeps_input_and_reports_not_ok():
    class Boom:
        def render(self, **kw):
            raise RuntimeError("boom")

    ring = SharedFrameRing(slots=1, slot_bytes=SLOT, prefix="cstest")
    try:
        (slot,) = ring.acquire(1)
        ring.write(slot, _frame(9))
        req_id, shape, _, ok = _serve_once(ring, (3, slot, (H, W, 3), [], "CLEAN", []), Boom())
        assert ok is False
        assert ring.read(slot, shape)[0, 0, 0] == 9   # 槽里仍是原始帧，主进程读到即「未标注原帧」
    finally:
        ring.close()
