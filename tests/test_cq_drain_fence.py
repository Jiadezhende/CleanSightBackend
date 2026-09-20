"""`ClientQueues.drain_ca_*` 的时间戳栅栏 —— 断流 flush 的切点。

栅栏是 P1-a（进重连时切出残帧段）的落点：重连**不拆除** CQ，所以 flush 的那一刻队列还在
进新帧。全排空会把重连后的帧跟断流前的帧拼进同一段，而 `effective_fps` 由首末帧跨度反推、
跨度里混着整个 gap —— 10 秒画面被写成 30 秒慢放，且 fps 仍落在合理带 [1,60] 内、不触发
退化兜底，全程无一条报警。

栅栏取"断流前最后一帧的 ts"，与调用时机、与此前拉走了多少整段都无关。
"""

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

    assert [f.timestamp for f in taken] == [1700.0, 1700.1, 1700.2]
    assert [f.timestamp for f in cq.ca_raw] == [1720.0, 1720.1], (
        "重连后的帧必须留在队列里，等 sweeper 照常拉整段"
    )


def test_fence_is_inclusive_of_the_boundary_frame():
    """栅栏值就是断流前最后一帧的 ts，那一帧必须被取走（闭区间）。"""
    cq = make_cq()
    _fill(cq, [1700.0, 1700.5])

    taken = cq.drain_ca_raw(until_ts=1700.5)

    assert len(taken) == 2
    assert len(cq.ca_raw) == 0


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
