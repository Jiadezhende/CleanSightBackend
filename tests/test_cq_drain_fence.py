"""帧队列（`RAW` / `PROCESSED` topic）的时间戳栅栏 —— 断流 flush 的切点。

栅栏是 P1-a（进重连时切出残帧段）的落点：重连**不拆除** run，所以 flush 的那一刻队列还在
进新帧。全排空会把重连后的帧跟断流前的帧拼进同一段，而 `effective_fps` 由首末帧跨度反推、
跨度里混着整个 gap —— 10 秒画面被写成 30 秒慢放，且 fps 仍落在合理带 [1,60] 内、不触发
退化兜底，全程无一条报警。

栅栏取"断流前最后一帧的 ts"，与调用时机、与此前拉走了多少整段都无关。

文件末尾附整段拉取：录制周期拉整段的另一条出队路径（drain 只切残段）。
"""

import pytest
from factories import make_cq, make_frame
from app.services.session.topics import PROCESSED, RAW

_TRACKS = pytest.mark.parametrize("track", [RAW, PROCESSED], ids=["raw", "processed"])


def _fill(cq, timestamps):
    for ts in timestamps:
        cq.sess.channel(RAW).push(make_frame(ts=ts))
        cq.sess.channel(PROCESSED).push(make_frame(ts=ts))


def _ts(frames):
    return [f.timestamp for f in frames]


@_TRACKS
def test_none_fence_drains_everything(track):
    """拆除期语义不变：`None` = 全排空（那时队列不会再进新帧）。"""
    cq = make_cq()
    _fill(cq, [1700.0, 1700.1, 1700.2])

    assert len(cq.sess.channel(track).drain()) == 3
    assert len(cq.sess.channel(track)) == 0


@_TRACKS
def test_fence_takes_only_the_prefix_before_it(track):
    cq = make_cq()
    _fill(cq, [1700.0, 1700.1, 1700.2, 1720.0, 1720.1])   # gap 20s
    q = cq.sess.channel(track)

    taken = q.drain(until_ts=1700.2)

    # 闭区间：栅栏值就是断流前最后一帧的 ts，那一帧必须被取走
    assert _ts(taken) == [1700.0, 1700.1, 1700.2]
    assert _ts(q.drain()) == [1720.0, 1720.1], "重连后的帧必须留在队列里，等 sweeper 照常拉整段"


@_TRACKS
def test_fence_before_everything_takes_nothing(track):
    cq = make_cq()
    _fill(cq, [1700.0, 1700.1])
    q = cq.sess.channel(track)

    assert q.drain(until_ts=1699.0) == []
    assert len(q) == 2


@_TRACKS
def test_fence_stops_at_the_first_frame_past_it(track):
    """只弹**队首连续前缀**，不是全队列筛选——队列本就按 ts 升序，前缀即全部合格帧。

    写成筛选会在乱序 ts 下从队列中间挖走帧，留下的段首尾跨度失真。
    """
    cq = make_cq()
    _fill(cq, [1700.0, 1720.0, 1700.1])   # 第三帧 ts 回退（不该发生，但别静默挖洞）
    q = cq.sess.channel(track)

    assert _ts(q.drain(until_ts=1700.5)) == [1700.0]
    assert _ts(q.drain()) == [1720.0, 1700.1]


# --- 整段拉取（录制周期拉取，攒满段长才出队）---


@_TRACKS
def test_take_segment_pops_exactly_seg_len(track):
    cq = make_cq()
    _fill(cq, [1.0, 2.0, 3.0, 4.0])
    q = cq.sess.channel(track)

    assert _ts(q.take(3)) == [1.0, 2.0, 3.0]
    assert _ts(q.drain()) == [4.0]


@_TRACKS
def test_take_segment_short_returns_none_and_keeps_residual(track):
    cq = make_cq()
    _fill(cq, [1.0, 2.0])
    q = cq.sess.channel(track)

    assert q.take(3) is None
    assert _ts(q.drain()) == [1.0, 2.0], "不足一段的残帧留给下一轮或断流 flush"


@_TRACKS
def test_take_segment_drains_backlog_segment_by_segment(track):
    """积压多段时，录制侧 `while take() is not None` 能按序逐段全部拉走。"""
    cq = make_cq()
    _fill(cq, [1.0, 2.0, 3.0, 4.0, 5.0, 6.0])
    q = cq.sess.channel(track)

    # 定次调用而非照抄 while：实现若回 [] 而非 None，照抄会死循环而不是红
    segs = [_ts(q.take(2)) for _ in range(3)]

    assert segs == [[1.0, 2.0], [3.0, 4.0], [5.0, 6.0]]
    assert q.take(2) is None
    assert len(q) == 0
