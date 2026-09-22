"""CQ 的 features 落盘缓冲（`ca_features`）—— 与 `ca_raw` / `ca_processed` 同族的第三条。

守四件事：写门（非 ACTIVE 拒）、满时丢最旧并计数、`drain` 原子全排空、`close` 释放。
它与 `_slide_window` 共用 `_slide_window_lock` 但**语义相反**：滑窗按算子感受野裁剪、会丢，
缓冲只在满时丢——所以两者不能合并，这里也各测各的。
"""

from factories import make_cq, make_frame_feature
from app.utils.metrics import frame_drop_total


def _feature_drops() -> float:
    return frame_drop_total.labels(reason="feature_backpressure")._value.get()


def test_append_then_drain_roundtrip():
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")
    for ts in (1.0, 2.0, 3.0):
        cq.append_ca_features(make_frame_feature(ts=ts))

    assert [f.ts for f in cq.drain_ca_features()] == [1.0, 2.0, 3.0]  # 保持写入序
    assert cq.drain_ca_features() == []                               # 取走即清，不重复交付


def test_full_buffer_drops_oldest_and_counts():
    cq = make_cq(task_id=1, step_id=2, source_ip="ip", ca_maxlen=2)
    before = _feature_drops()

    for ts in (1.0, 2.0, 3.0):
        cq.append_ca_features(make_frame_feature(ts=ts))

    assert [f.ts for f in cq.drain_ca_features()] == [2.0, 3.0]  # 最旧那帧被挤掉
    assert _feature_drops() - before == 1.0


def test_draining_run_rejects_writes():
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")
    cq.to_draining()

    cq.append_ca_features(make_frame_feature(ts=1.0))

    assert cq.drain_ca_features() == []


def test_close_releases_the_buffer():
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")
    cq.append_ca_features(make_frame_feature(ts=1.0))

    cq.close()

    assert cq.drain_ca_features() == []
    assert cq.get_queue_depths()["ca_features"] == 0


def test_slide_window_and_buffer_are_independent():
    """滑窗被裁不影响缓冲：落盘序列不该随算子感受野缩水。"""
    cq = make_cq(task_id=1, step_id=2, source_ip="ip")
    cq.set_stream_windows({"op": 0.0})   # 底线 10s 感受野

    for ts in (1.0, 2.0, 100.0):         # 第三帧把前两帧挤出滑窗
        cq.push_detection(make_frame_feature(ts=ts))
        cq.append_ca_features(make_frame_feature(ts=ts))

    assert [f.ts for f in cq.get_slide_window()] == [100.0]
    assert [f.ts for f in cq.drain_ca_features()] == [1.0, 2.0, 100.0]
