"""`ClientQueues.drain_ca_*` 的时间戳栅栏 —— 断流 flush 的切点。

栅栏是 P1-a（进重连时切出残帧段）的落点：重连**不拆除** CQ，所以 flush 的那一刻队列还在
进新帧。全排空会把重连后的帧跟断流前的帧拼进同一段，而 `effective_fps` 由首末帧跨度反推、
跨度里混着整个 gap —— 10 秒画面被写成 30 秒慢放，且 fps 仍落在合理带 [1,60] 内、不触发
退化兜底，全程无一条报警。

栅栏取"断流前最后一帧的 ts"，与调用时机、与此前拉走了多少整段都无关。

文件末尾附 `take_*_segment`：录制周期拉整段的另一条出队路径（drain 只切残段）。
"""

import pytest
from factories import make_cq, make_frame


def _fill(cq, timestamps):
    for ts in timestamps:
        cq.append_ca_raw(make_frame(ts=ts))
        cq.append_ca_processed(make_frame(ts=ts))


def test_none_fence_drains_everything():
    """拆除期语义不变：`None` = 全排空（那时队列不会再进新帧）。"""
    cq = make_cq()
    _fill(cq, [1700.0, 1700.1, 1700.2])

    assert len(cq.drain_ca_raw()) == 3
    assert len(cq.drain_ca_processed()) == 3
    assert len(cq.ca_raw) == 0
    assert len(cq.ca_processed) == 0


def test_fence_takes_only_the_prefix_before_it():
    cq = make_cq()
    _fill(cq, [1700.0, 1700.1, 1700.2, 1720.0, 1720.1])   # gap 20s

    taken = cq.drain_ca_raw(until_ts=1700.2)

    # 闭区间：栅栏值就是断流前最后一帧的 ts，那一帧必须被取走
    assert [f.timestamp for f in taken] == [1700.0, 1700.1, 1700.2]
    assert [f.timestamp for f in cq.ca_raw] == [1720.0, 1720.1], (
        "重连后的帧必须留在队列里，等 sweeper 照常拉整段"
    )


def test_fence_before_everything_takes_nothing():
    cq = make_cq()
    _fill(cq, [1700.0, 1700.1])

    assert cq.drain_ca_raw(until_ts=1699.0) == []
    assert len(cq.ca_raw) == 2


def test_fence_stops_at_the_first_frame_past_it():
    """只弹**队首连续前缀**，不是全队列筛选——队列本就按 ts 升序，前缀即全部合格帧。

    写成筛选会在乱序 ts 下从队列中间挖走帧，留下的段首尾跨度失真。
    """
    cq = make_cq()
    _fill(cq, [1700.0, 1720.0, 1700.1])   # 第三帧 ts 回退（不该发生，但别静默挖洞）

    taken = cq.drain_ca_raw(until_ts=1700.5)

    assert [f.timestamp for f in taken] == [1700.0]
    assert [f.timestamp for f in cq.ca_raw] == [1720.0, 1700.1]


def test_processed_track_uses_the_same_fence():
    cq = make_cq()
    _fill(cq, [1700.0, 1700.1, 1720.0])

    taken = cq.drain_ca_processed(until_ts=1700.1)

    assert [f.timestamp for f in taken] == [1700.0, 1700.1]
    assert [f.timestamp for f in cq.ca_processed] == [1720.0]


# --- 整段拉取：take_*_segment（录制周期拉取，攒满 ca_segment_len 才出队）---

_TRACKS = pytest.mark.parametrize(
    "take, queue", [("take_raw_segment", "ca_raw"), ("take_processed_segment", "ca_processed")],
)


def _ts(frames):
    return [f.timestamp for f in frames]


@_TRACKS
def test_take_segment_pops_exactly_seg_len(take, queue):
    cq = make_cq(ca_segment_len=3)
    _fill(cq, [1.0, 2.0, 3.0, 4.0])

    assert _ts(getattr(cq, take)()) == [1.0, 2.0, 3.0]
    assert _ts(getattr(cq, queue)) == [4.0]


@_TRACKS
def test_take_segment_short_returns_none_and_keeps_residual(take, queue):
    cq = make_cq(ca_segment_len=3)
    _fill(cq, [1.0, 2.0])

    assert getattr(cq, take)() is None
    assert _ts(getattr(cq, queue)) == [1.0, 2.0], "不足一段的残帧留给下一轮或断流 flush"


@_TRACKS
def test_take_segment_drains_backlog_segment_by_segment(take, queue):
    """积压多段时，录制侧 `while take() is not None` 能按序逐段全部拉走。"""
    cq = make_cq(ca_segment_len=2)
    _fill(cq, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])

    # 定次调用而非照抄 while：实现若回 [] 而非 None，照抄会死循环而不是红
    segs = [_ts(getattr(cq, take)()) for _ in range(3)]

    assert segs == [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
    assert getattr(cq, take)() is None
    assert len(getattr(cq, queue)) == 0
