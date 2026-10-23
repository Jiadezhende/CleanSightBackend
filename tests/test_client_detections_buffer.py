"""detections 落盘缓冲（`DETECTIONS` topic）—— 与 `RAW` / `PROCESSED` 同族的第三条。

守四件事：写门（非 ACTIVE 拒）、满时丢最旧并计数、`drain` 原子全排空、`close` 释放。
它与帧窗 `DETECTION_WINDOW` 是同一帧的两个去处，但**语义相反**：帧窗按算子感受野裁剪、会丢，
缓冲只在满时丢——所以两者不能合并，这里也各测各的。
"""

from factories import make_cq, make_frame_detection
from app.services.inference.online.topics import DETECTION_WINDOW
from app.services.session.topics import DETECTIONS
from app.services.utils.metrics import frame_drop_total


def _detection_drops() -> float:
    return frame_drop_total.labels(reason="detection_backpressure")._value.get()


def _buf(cq):
    return cq.sess.channel(DETECTIONS)


def test_append_then_drain_roundtrip():
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")
    for ts in (1.0, 2.0, 3.0):
        _buf(cq).push(make_frame_detection(ts=ts))

    assert [f.ts for f in _buf(cq).drain()] == [1.0, 2.0, 3.0]  # 保持写入序
    assert _buf(cq).drain() == []                                # 取走即清，不重复交付


def test_full_buffer_drops_oldest_and_counts(topic_sizes):
    topic_sizes(ca_maxlen=2)
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")
    before = _detection_drops()

    for ts in (1.0, 2.0, 3.0):
        _buf(cq).push(make_frame_detection(ts=ts))

    assert [f.ts for f in _buf(cq).drain()] == [2.0, 3.0]  # 最旧那帧被挤掉
    assert _detection_drops() - before == 1.0


def test_draining_run_rejects_writes():
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")
    cq.to_draining()

    _buf(cq).push(make_frame_detection(ts=1.0))

    assert _buf(cq).drain() == []


def test_close_releases_the_buffer():
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")
    _buf(cq).push(make_frame_detection(ts=1.0))

    cq.close()

    assert _buf(cq).drain() == []
    assert cq.get_queue_depths()["ca_detections"] == 0


def test_slide_window_and_buffer_are_independent():
    """帧窗被裁不影响缓冲：落盘序列不该随算子感受野缩水。"""
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")

    for ts in (1.0, 2.0, 100.0):         # 第三帧把前两帧挤出 10s 帧窗
        cq.sess.channel(DETECTION_WINDOW).push(make_frame_detection(ts=ts))
        _buf(cq).push(make_frame_detection(ts=ts))

    assert [f.ts for f in cq.sess.channel(DETECTION_WINDOW).snapshot()] == [100.0]
    assert [f.ts for f in _buf(cq).drain()] == [1.0, 2.0, 100.0]
