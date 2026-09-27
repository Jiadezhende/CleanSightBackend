"""resample_by_ts：按 ts 相位网格降采样（在线 Operator / 离线 Segmenter 共用）。"""

import pytest
from factories import make_frame_detection

from app.services.inference.resample import resample_by_ts


def _frames(ts_list):
    return [make_frame_detection(ts=t) for t in ts_list]


def _ts(frames):
    return [round(f.ts, 6) for f in frames]


def _gaps(frames, src_fps):
    """相邻保留帧间隔（以源帧数计）的集合。"""
    return {round((b.ts - a.ts) * src_fps) for a, b in zip(frames, frames[1:])}


def test_halves_15fps_to_7_5fps_every_other_frame():
    """ts 按存储格式保留 6 位小数：网格点与帧重合，舍入不得让间隔抖成 1/3 帧。"""
    frames = _frames([round(i / 15, 6) for i in range(300)])
    kept = resample_by_ts(frames, 7.5)
    assert len(kept) == 150
    assert _gaps(kept, 15) == {2}


def test_equal_rate_keeps_all_frames():
    frames = _frames([round(i / 7.5, 6) for i in range(72)])
    assert resample_by_ts(frames, 7.5) == frames


def test_jittered_ts_still_every_other_frame():
    jitter = [0.004, -0.003, 0.002, -0.004, 0.001, -0.002]
    frames = _frames([i / 15 + jitter[i % len(jitter)] for i in range(300)])
    assert _gaps(resample_by_ts(frames, 7.5), 15) == {2}


def test_gap_reanchors_instead_of_catching_up():
    frames = _frames([0.0, 1 / 15, 2 / 15, 1.0, 1.0 + 1 / 15, 1.0 + 2 / 15])
    assert _ts(resample_by_ts(frames, 7.5)) == [0.0, round(2 / 15, 6), 1.0, round(1.0 + 2 / 15, 6)]


def test_target_above_input_rate_keeps_all_when_lenient():
    frames = _frames([0.0, 0.5, 1.0])
    assert resample_by_ts(frames, 7.5) == frames


def test_strict_rejects_input_slower_than_target():
    frames = _frames([i / 5 for i in range(20)])
    with pytest.raises(ValueError, match="低于契约帧率"):
        resample_by_ts(frames, 7.5, strict=True)


def test_strict_accepts_equal_rate_with_rounded_ts():
    frames = _frames([round(i / 7.5, 6) for i in range(72)])
    assert resample_by_ts(frames, 7.5, strict=True) == frames


def test_fewer_than_two_frames_returned_as_is():
    assert resample_by_ts([], 7.5) == []
    one = _frames([0.3])
    assert resample_by_ts(one, 7.5) == one


def test_frames_are_not_copied_or_modified():
    frames = _frames([0.0, 1 / 15, 2 / 15])
    kept = resample_by_ts(frames, 7.5)
    assert kept[0] is frames[0] and kept[1] is frames[2]
