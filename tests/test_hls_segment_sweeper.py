"""HLSSegmentSweeper（PULL 分段落盘）的行为守卫。

覆盖不变式：
1. _sweep 把攒满的整段（ca_segment_len 帧）拉走、按 raw/processed 分别入队，段数/段长正确；
2. 不足一段的残帧留在缓冲，不落盘（残段由 flush_residual_segments 收尾）；
3. step_id 为 None 的 CQ 被跳过（无法定位落盘分区）；
4. 拉取后缓冲被排空（下一轮不重复落同一批帧）。
"""

from factories import make_bare_cq, make_cq, make_frame

from app.services.persistence.workers.segment_sweeper import HLSSegmentSweeper


def _sweeper(snapshot, calls):
    """构造仅用于 _sweep 单测的 sweeper：注入固定快照与捕获式 persist_fn。"""
    def persist_fn(**kw):
        calls.append(kw)
        return True

    return HLSSegmentSweeper(snapshot_fn=lambda: snapshot, persist_fn=persist_fn)


def _fill(cq, append_fn, n):
    for i in range(n):
        append_fn(make_frame(ts=float(i)))


def test_sweep_pulls_full_segments_by_seg_len():
    # seg_len=10，灌 25 raw + 12 processed → raw 2 段(各 10)、processed 1 段(10)，余帧留缓冲
    cq = make_cq(ca_segment_len=10, ca_maxlen=100)
    _fill(cq, cq.append_ca_raw, 25)
    _fill(cq, cq.append_ca_processed, 12)

    calls = []
    _sweeper({1: cq}, calls)._sweep()

    raw = [c for c in calls if c["segment_type"] == "raw"]
    proc = [c for c in calls if c["segment_type"] == "processed"]
    assert [len(c["frames"]) for c in raw] == [10, 10]
    assert [len(c["frames"]) for c in proc] == [10]
    assert all(c["task_id"] == 1 and c["step_id"] == 1 for c in calls)

    # 拉取后缓冲只剩不足一段的残帧，且不再被落盘
    assert len(cq.ca_raw) == 5
    assert cq.get_ca_processed_length() == 2


def test_sweep_no_full_segment_persists_nothing():
    cq = make_cq(ca_segment_len=10, ca_maxlen=100)
    _fill(cq, cq.append_ca_raw, 9)  # 不足一段

    calls = []
    _sweeper({1: cq}, calls)._sweep()

    assert calls == []
    assert len(cq.ca_raw) == 9  # 残帧原样留缓冲


def test_sweep_skips_cq_without_step_id():
    # 裸建 CQ：step_id=None → 跳过（即便攒满也不落盘）
    cq = make_bare_cq(ca_segment_len=10, ca_maxlen=100)
    assert cq.step_id is None
    _fill(cq, cq.append_ca_raw, 20)

    calls = []
    _sweeper({7: cq}, calls)._sweep()

    assert calls == []
    assert len(cq.ca_raw) == 20


def test_sweep_drains_backlog_multiple_segments():
    # 一次 sweep 内把积压的多整段全部拉走（while 循环 drain backlog）
    cq = make_cq(ca_segment_len=5, ca_maxlen=100)
    _fill(cq, cq.append_ca_raw, 23)  # 4 整段 + 3 残帧

    calls = []
    _sweeper({1: cq}, calls)._sweep()

    raw = [c for c in calls if c["segment_type"] == "raw"]
    assert len(raw) == 4
    assert all(len(c["frames"]) == 5 for c in raw)
    assert len(cq.ca_raw) == 3
