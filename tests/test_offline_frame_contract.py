import numpy as np
import pytest

from factories import make_frame_detection

from app.services.inference.offline.segmentation.utils import FrameInputContract, prepare_frames


_DINO = FrameInputContract(target_fps=15.0, unique_ts=True, expected_size=(640, 480))


def _frames(timestamps, *, width=640, height=480):
    return [make_frame_detection(ts=ts, frame_width=width, frame_height=height) for ts in timestamps]


def test_truncation_is_inclusive_and_precedes_rate_check(caplog):
    times = [1000.0, 1000.2, 1000.4] + [1000.4 + i / 20 for i in range(1, 20)]
    frames = _frames(times)
    kept = prepare_frames(frames, _DINO, max_duration_s=0.4, producer="DINO")
    assert len(kept) == 3
    assert all(actual is original for actual, original in zip(kept, frames))
    assert "只推理前" in caplog.text and "输入帧率 5.000fps" in caplog.text


def test_unset_contract_preserves_original_sequence():
    frames = _frames([0.0, 0.0, 0.1], width=1280, height=720)
    assert prepare_frames(frames, FrameInputContract(), producer="v2") is frames


def test_low_rate_warning_keeps_frames_and_duplicate_ts_uses_first(caplog):
    frames = _frames([0.0, 0.2, 0.2, 0.4])
    kept = prepare_frames(frames, _DINO, producer="DINO")
    assert [frame.ts for frame in kept] == [0.0, 0.2, 0.4]
    assert kept[1] is frames[1]
    assert "低于训练帧率" in caplog.text and "重复 ts" in caplog.text


def test_strict_rate_rejects_slow_input():
    contract = FrameInputContract(target_fps=7.5, low_rate="error")
    with pytest.raises(ValueError, match="低于契约帧率"):
        prepare_frames(_frames([0.0, 0.2, 0.4]), contract, producer="GRU")


def test_rate_check_uses_steady_mean_and_preserves_original_ts(caplog):
    times = np.cumsum([0.0] + [0.08, 0.08, 0.04] * 15)
    times[25:] += 30.0  # 断流缺口不计入检测帧率
    frames = _frames(times.tolist())
    kept = prepare_frames(frames, _DINO, producer="DINO")
    assert len(kept) > 0 and caplog.text == ""
    assert {id(frame) for frame in kept} <= {id(frame) for frame in frames}


def test_missing_dimensions_are_accepted_and_only_retained_frames_are_checked():
    frames = _frames([0.0, 0.1], width=None, height=None) + _frames([1.0], width=1280, height=720)
    kept = prepare_frames(frames, _DINO, max_duration_s=0.1, producer="DINO")
    assert [frame.ts for frame in kept] == [0.0, 0.1]


@pytest.mark.parametrize("width,height", [(1280, 720), (640, None)])
def test_incompatible_dimensions_are_rejected(width, height):
    with pytest.raises(ValueError, match="640×480"):
        prepare_frames(_frames([0.0], width=width, height=height), _DINO, producer="DINO")
