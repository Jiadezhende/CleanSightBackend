"""SharedFrameRing：槽位分配原子性、拷入拷出往返、跨进程读、回收与销毁。"""

import multiprocessing as mp

import numpy as np
import pytest

from app.services.utils.frame_ring import SharedFrameRing, SharedFrameRingView, SharedFrames

SHAPE = (4, 6, 3)
SLOT = 4 * 6 * 3


@pytest.fixture
def ring():
    r = SharedFrameRing(slots=4, slot_bytes=SLOT, prefix="cstest")
    yield r
    r.close()


def _frame(v):
    return np.full(SHAPE, v, dtype=np.uint8)


def test_acquire_is_all_or_nothing(ring):
    assert ring.acquire(3) is not None
    assert ring.acquire(2) is None          # 只剩 1 个，不部分分配
    assert ring.free_count == 1


def test_release_and_reset_return_slots(ring):
    a = ring.acquire(2)
    ring.acquire(2)
    ring.release(a)
    assert ring.free_count == 2
    ring.reset()
    assert ring.free_count == 4


def test_write_read_roundtrip_returns_independent_copy(ring):
    view = SharedFrameRingView(ring.spec)
    try:
        slots = ring.acquire(2)
        ring.write(slots[0], _frame(7))
        ring.write(slots[1], _frame(9))
        got = view.read_all(SharedFrames(ring.spec, tuple(slots), (SHAPE, SHAPE)))
        assert got[0][0, 0, 0] == 7 and got[1][0, 0, 0] == 9

        ring.write(slots[0], _frame(1))     # 槽位被复用，已读出的副本不受影响
        assert got[0][0, 0, 0] == 7
    finally:
        view.close()


def test_non_contiguous_frame_written_correctly(ring):
    base = np.arange(4 * 12 * 3, dtype=np.uint8).reshape(4, 12, 3)
    frame = base[:, ::2]                    # 非连续视图
    (slot,) = ring.acquire(1)
    ring.write(slot, frame)
    view = SharedFrameRingView(ring.spec)
    try:
        assert np.array_equal(view.read(slot, frame.shape), frame)
    finally:
        view.close()


def test_oversize_or_wrong_dtype_rejected(ring):
    assert not ring.fits(np.zeros((10, 10, 3), dtype=np.uint8))
    assert not ring.fits(np.zeros(SHAPE, dtype=np.float32))
    (slot,) = ring.acquire(1)
    with pytest.raises(ValueError):
        ring.write(slot, np.zeros((10, 10, 3), dtype=np.uint8))


def test_close_is_idempotent_and_blocks_acquire():
    r = SharedFrameRing(slots=2, slot_bytes=SLOT, prefix="cstest")
    r.close()
    r.close()
    assert r.acquire(1) is None


def _child_sum(spec, slots, shapes, out):
    view = SharedFrameRingView(spec)
    out.put([int(f.sum()) for f in view.read_all(SharedFrames(spec, slots, shapes))])
    view.close()


def test_spawned_process_reads_frames(ring):
    slots = ring.acquire(2)
    ring.write(slots[0], _frame(1))
    ring.write(slots[1], _frame(2))
    ctx = mp.get_context("spawn")
    out = ctx.Queue()
    p = ctx.Process(target=_child_sum, args=(ring.spec, tuple(slots), (SHAPE, SHAPE), out))
    p.start()
    try:
        assert out.get(timeout=60) == [SLOT * 1, SLOT * 2]
    finally:
        p.join(timeout=10)
