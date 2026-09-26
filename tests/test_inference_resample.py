"""resample_by_ts：按 ts 相位网格降采样（在线 Operator / 离线 Segmenter 共用）。"""

import pytest
from factories import make_frame_detection

from app.services.inference.resample import resample_by_ts


def _frames(ts_list):
    return [make_frame_detection(ts=t) for t in ts_list]


def _ts(frames):
    return [round(f.ts, 6) for f in frames]


def test_halves_15fps_to_7_5fps_without_drift():
    frames = _frames([i / 15 for i in range(300)])
    kept = resample_by_ts(frames, 7.5)
    # 浮点累加会偶发单帧相位滑动（间隔 3/15 紧跟 1/15），但帧数与平均帧率不漂
    assert len(kept) == 150
    assert (kept[-1].ts - kept[0].ts) / (len(kept) - 1) == pytest.approx(2 / 15, rel=1e-2)


def test_gap_reanchors_instead_of_catching_up():
    frames = _frames([0.0, 1 / 15, 2 / 15, 1.0, 1.0 + 1 / 15, 1.0 + 2 / 15])
    assert _ts(resample_by_ts(frames, 7.5)) == [0.0, round(2 / 15, 6), 1.0, round(1.0 + 2 / 15, 6)]


def test_target_above_input_rate_keeps_all():
    frames = _frames([0.0, 0.5, 1.0])
    assert resample_by_ts(frames, 7.5) == frames


def test_fewer_than_two_frames_returned_as_is():
    assert resample_by_ts([], 7.5) == []
    one = _frames([0.3])
    assert resample_by_ts(one, 7.5) == one


def test_frames_are_not_copied_or_modified():
    frames = _frames([0.0, 1 / 15, 2 / 15])
    kept = resample_by_ts(frames, 7.5)
    assert kept[0] is frames[0] and kept[1] is frames[2]
