"""
FrameTracker / Timeline 边界单元测试（seam：不起 ffmpeg）

段级 + 帧级裁剪是纯 searchsorted 数学，把 `_run_ffmpeg` 这个 I/O 边界替换成
「按 sidecar 合成 Frame」的 seam，就能不依赖 ffmpeg 覆盖全部边界情形。
真实解码（ts ↔ 像素是否错配）由 integration_tests/test_frame_tracker_roundtrip.py 端到端验。

覆盖：
- 段级：起点落段中部 / 起点恰为段首帧 / 跨段 / 默认区间含末段 / 区间早于首段 / 晚于末段
- 帧级：区间落两帧之间 / 跨段接缝相邻帧 / 单帧区间
- 缺 sidecar：跳过该段而非打断整条迭代
- find：多点、重复 ts、ts 漂移即失败、空入参
- _build_cmd：select 必须在 scale 之前

落盘约定：{base_dir}/{task_id}/{step_id}/raw_segment_{ts_us}.mp4 + 同名 .idx
"""

from pathlib import Path
from typing import Iterator, List

import numpy as np
import pytest

from app.domain.frame import Frame
from app.services.inference.offline.frame_tracker import FrameTracker, Timeline

TASK_ID = 4242
STEP_ID = 7
FPS = 15.0
FRAMES_PER_SEG = 10
N_SEG = 4
BASE_TS = 1786731122.204701


def ts_of(gid: int) -> float:
    """全局帧号 → ts。与真实链路同款：非等距（带确定性抖动）。"""
    return BASE_TS + gid / FPS + 0.004 * np.sin(gid * 1.7)


def seg_frames(s: int) -> List[float]:
    return [ts_of(s * FRAMES_PER_SEG + i) for i in range(FRAMES_PER_SEG)]


class FakeDecodeTimeline(Timeline):
    """把 ffmpeg 解码换成「按 sidecar 合成 1×1 帧」，段内帧号与 sidecar 下标仍 1:1。

    真实 `_run_ffmpeg` 的契约就是「产出 sidecar[k_start..k_end] 对应的帧」，
    这里如实复刻该契约，故所有裁剪边界逻辑都被真实覆盖。
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.calls: List[tuple] = []  # 记录 (段文件名, k_start, k_end)，验段级裁剪

    def _run_ffmpeg(self, seg, sidecar, k_start, k_end, width, height) -> Iterator[Frame]:
        self.calls.append((seg.filename, k_start, k_end))
        for k in range(k_start, k_end + 1):
            yield Frame(
                timestamp=float(sidecar[k]),
                frame=np.zeros((height, width, 3), dtype=np.uint8),
            )


@pytest.fixture
def step_dir(tmp_storage) -> Path:
    """造 N_SEG 段：空 mp4 占位（SegmentFinder 只解析文件名）+ 真 sidecar。"""
    d = tmp_storage / str(TASK_ID) / str(STEP_ID)
    d.mkdir(parents=True, exist_ok=True)
    for s in range(N_SEG):
        tss = seg_frames(s)
        ts_us = int(tss[0] * 1e6)
        (d / f"raw_segment_{ts_us}.mp4").write_bytes(b"")
        np.array(tss, dtype=np.float64).tofile(d / f"raw_segment_{ts_us}.idx")
    return d


@pytest.fixture
def tl(step_dir) -> FakeDecodeTimeline:
    return FakeDecodeTimeline(TASK_ID, STEP_ID)


def ts_out(tl: Timeline, *args, **kwargs) -> List[float]:
    return [f.timestamp for f in tl.iter(*args, **kwargs)]


class TestSegmentLevelTrim:
    def test_full_sweep_includes_last_segment(self, tl):
        """默认区间必须含末段全部帧 —— 段起始数组的末元素是**末段段首**，
        拿它当时间轴末端会把末段砍到只剩第一帧。"""
        assert ts_out(tl) == [ts_of(g) for g in range(N_SEG * FRAMES_PER_SEG)]

    def test_start_inside_segment_keeps_that_segment(self, tl):
        """起点落在段中部：包含它的那一段不能被跳过。"""
        g = FRAMES_PER_SEG + 4
        assert ts_out(tl, ts_of(g), ts_of(g + 3)) == [ts_of(k) for k in range(g, g + 4)]

    def test_start_exactly_on_segment_first_frame(self, tl):
        """起点恰为段首帧：文件名 ts_us = int(ts*1e6) 截断 → start*1e6 > ts_us，
        用 side='left' 会连这一段一起跳过。"""
        g = 2 * FRAMES_PER_SEG
        assert ts_out(tl, ts_of(g), ts_of(g + 2)) == [ts_of(k) for k in range(g, g + 3)]

    def test_cross_segment_range(self, tl):
        g0, g1 = FRAMES_PER_SEG + 6, 3 * FRAMES_PER_SEG + 2
        assert ts_out(tl, ts_of(g0), ts_of(g1)) == [ts_of(k) for k in range(g0, g1 + 1)]
        # 只该碰 3 段，首段不入选
        assert len(tl.calls) == 3

    def test_range_entirely_before_first_segment(self, tl):
        """end_ts 早于首段起点 → hi = -1，不能被 clamp 成 0 后误出第 0 帧。"""
        assert ts_out(tl, BASE_TS - 100, BASE_TS - 50) == []
        assert tl.calls == []

    def test_range_entirely_after_last_frame(self, tl):
        last = ts_of(N_SEG * FRAMES_PER_SEG - 1)
        assert ts_out(tl, last + 50, last + 100) == []

    def test_start_before_first_segment_clamps_to_head(self, tl):
        got = ts_out(tl, BASE_TS - 100, ts_of(2))
        assert got == [ts_of(0), ts_of(1), ts_of(2)]

    def test_open_ended_start_and_end(self, tl):
        g = 2 * FRAMES_PER_SEG + 3
        assert ts_out(tl, None, ts_of(2)) == [ts_of(0), ts_of(1), ts_of(2)]
        assert ts_out(tl, ts_of(g), None) == [
            ts_of(k) for k in range(g, N_SEG * FRAMES_PER_SEG)
        ]


class TestFrameLevelTrim:
    def test_single_frame_range(self, tl):
        g = FRAMES_PER_SEG + 5
        assert ts_out(tl, ts_of(g), ts_of(g)) == [ts_of(g)]

    def test_range_between_two_frames_is_empty(self, tl):
        """区间完全落在相邻两帧之间 → 空，且不能误出边上的帧。"""
        g = FRAMES_PER_SEG + 5
        lo = ts_of(g) + 1e-3
        hi = ts_of(g + 1) - 1e-3
        assert lo < hi  # 前提：确实存在这么个空隙
        assert ts_out(tl, lo, hi) == []

    def test_segment_seam_adjacent_frames(self, tl):
        """跨段接缝上的相邻两帧：前一段末帧 + 后一段首帧。"""
        g = FRAMES_PER_SEG - 1
        assert ts_out(tl, ts_of(g), ts_of(g + 1)) == [ts_of(g), ts_of(g + 1)]

    def test_trims_within_single_segment(self, tl):
        g0, g1 = 2, 5
        ts_out(tl, ts_of(g0), ts_of(g1))
        assert tl.calls == [(tl._segs[0].filename, g0, g1)]


class TestMissingSidecar:
    def test_skips_segment_without_sidecar(self, tl, step_dir):
        """缺一段的索引只该丢那一段，不该让前后所有段一起读不了。"""
        victim = sorted(step_dir.glob("raw_segment_*.idx"))[1]
        victim.unlink()

        got = ts_out(tl)
        expected = [
            ts_of(g)
            for g in range(N_SEG * FRAMES_PER_SEG)
            if not (FRAMES_PER_SEG <= g < 2 * FRAMES_PER_SEG)
        ]
        assert got == expected

    def test_empty_timeline_yields_nothing(self, tmp_storage):
        assert list(Timeline(999, 999).iter()) == []


class TestFind:
    @pytest.fixture(autouse=True)
    def _patch_tracker(self, monkeypatch, step_dir):
        """FrameTracker 内部自建 Timeline，这里换成 seam 版。"""
        monkeypatch.setattr(
            "app.services.inference.offline.frame_tracker.Timeline", FakeDecodeTimeline
        )

    def test_multi_point_across_segments(self):
        gids = [1, 13, 27, 39]
        got = list(FrameTracker(TASK_ID, STEP_ID).find([ts_of(g) for g in gids], 4, 4))
        assert [f.timestamp for f in got] == [ts_of(g) for g in gids]

    def test_returns_ts_ascending_not_input_order(self):
        gids = [27, 1, 13]
        got = list(FrameTracker(TASK_ID, STEP_ID).find([ts_of(g) for g in gids], 4, 4))
        assert [f.timestamp for f in got] == [ts_of(g) for g in sorted(gids)]

    def test_duplicate_ts_yields_one_frame_each(self):
        g = 17
        got = list(FrameTracker(TASK_ID, STEP_ID).find([ts_of(g), ts_of(g)], 4, 4))
        assert [f.timestamp for f in got] == [ts_of(g), ts_of(g)]

    @pytest.mark.parametrize("drift", [1e-6, -1e-6, 1e-3])
    def test_drifted_ts_raises(self, drift):
        """ts 是帧的身份，不做近似匹配：配错帧比报错更坏。"""
        with pytest.raises(ValueError, match="未找到 ts="):
            list(FrameTracker(TASK_ID, STEP_ID).find([ts_of(17) + drift], 4, 4))

    def test_ts_outside_timeline_raises(self):
        with pytest.raises(ValueError, match="未找到 ts="):
            list(FrameTracker(TASK_ID, STEP_ID).find([BASE_TS + 9999], 4, 4))

    def test_empty_input_yields_nothing(self):
        assert list(FrameTracker(TASK_ID, STEP_ID).find([], 4, 4)) == []


class TestBuildCmd:
    def test_select_precedes_scale(self, tl):
        """scale 在 select 之前会把注定被丢弃的帧也缩放一遍。"""
        cmd = tl._build_cmd(tl._segs[0], 3, 7, 320, 240)
        vf = cmd[cmd.index("-vf") + 1]
        assert vf.index("select=") < vf.index("scale=")
        assert "between(n\\,3\\,7)" in vf
        assert cmd[cmd.index("-vframes") + 1] == "5"

    def test_concat_uses_track_init(self, tl):
        cmd = tl._build_cmd(tl._segs[0], 0, 0, 4, 4)
        src = cmd[cmd.index("-i") + 1]
        assert src.startswith("concat:")
        assert "raw_init.mp4" in src
        assert tl._segs[0].filename in src
