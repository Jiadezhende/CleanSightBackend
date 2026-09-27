"""
FrameTracker 边界单元测试（seam：不起 ffmpeg）

`FrameTracker` 调 `app.storage.hls.iter_frames`，读 `{step}/hls/`；造数走 `hls.insert_segment`
（写侧同源，不手搓文件名），seam 打在 `hls._decode._run_ffmpeg` 上，按 sidecar 合成 Frame。
真实解码（ts ↔ 像素是否错配）由 integration_tests/test_frame_tracker_roundtrip.py 端到端验。

覆盖 find：多点、升序契约、重复 ts、ts 漂移即失败、越界、空入参、位级相等。

`hls.iter_frames` 自己的两级裁剪边界由 tests/test_storage_hls.py 覆盖，此处不重复。
"""

import struct
from pathlib import Path
from typing import List

import numpy as np
import pytest

from app.domain.frame import Frame
from app.services.inference.offline.frame_tracker import FrameTracker
from app.storage import hls
from app.storage.hls import _decode, _encode, _fmp4

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


def _box(typ: bytes, body: bytes) -> bytes:
    return struct.pack(">I", 8 + len(body)) + typ + body


def _fragment_bytes() -> bytes:
    """最小合法 fMP4 fragment（`moof/traf/tfdt` v1 + mdat）—— 只为让 tfdt 修补有东西可改。"""
    tfdt_body = b"\x01\x00\x00\x00" + struct.pack(">Q", 0)
    return _box(b"moof", _box(b"traf", _box(b"tfdt", tfdt_body))) + _box(b"mdat", b"\x00" * 16)


@pytest.fixture
def hls_step(tmp_storage) -> Path:
    """走真实 `hls.insert_segment` 铺 `{step}/hls/`，只把 cv2 / ffmpeg 两步换成假的。

    刻意不手搓文件名：布局与命名归写侧，测试跟着它走才不会两边各对各的。
    """

    def _write_mp4v(path, frames, fps):
        path.write_bytes(b"mp4v-source")

    def _transcode(stage):
        fragment = stage / "fragment_0.mp4"
        fragment.write_bytes(_fragment_bytes())
        init = stage / "init.mp4"
        init.write_bytes(b"fake-init")
        return fragment, init

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(_encode, "write_mp4v", _write_mp4v)
        mp.setattr(_fmp4, "transcode", _transcode)
        for s in range(N_SEG):
            hls.insert_segment(
                TASK_ID, STEP_ID, "raw",
                [
                    Frame(timestamp=t, frame=np.zeros((4, 4, 3), dtype=np.uint8))
                    for t in seg_frames(s)
                ],
            )
    return tmp_storage / str(TASK_ID) / str(STEP_ID) / "hls"


@pytest.fixture
def fake_decode(monkeypatch) -> None:
    """把数据层的解码 I/O 边界换成「按 sidecar 合成帧」，契约同真实 `_run_ffmpeg`。"""

    def _fake(task_id, step_id, ref, sidecar, k_start, k_end, width, height):
        for k in range(k_start, k_end + 1):
            yield Frame(
                timestamp=float(sidecar[k]),
                frame=np.zeros((height, width, 3), dtype=np.uint8),
            )

    monkeypatch.setattr(_decode, "_run_ffmpeg", _fake)


class TestFind:
    def test_multi_point_across_segments(self, hls_step, fake_decode):
        gids = [1, 13, 27, 39]
        got = list(FrameTracker(TASK_ID, STEP_ID).find([ts_of(g) for g in gids], 4, 4))
        assert [f.timestamp for f in got] == [ts_of(g) for g in gids]

    def test_matched_ts_are_bit_exact(self, hls_step, fake_decode):
        """sidecar 存 float64 原值，反查回来的 ts 必须与内存里那个**位级**相等。"""
        wanted = [ts_of(g) for g in (0, 17, 39)]
        got = [f.timestamp for f in FrameTracker(TASK_ID, STEP_ID).find(wanted, 4, 4)]
        assert all(a == b for a, b in zip(got, wanted))
        assert [f.hex() for f in got] == [w.hex() for w in wanted]

    def test_returns_ts_ascending_not_input_order(self, hls_step, fake_decode):
        gids = [27, 1, 13]
        got = list(FrameTracker(TASK_ID, STEP_ID).find([ts_of(g) for g in gids], 4, 4))
        assert [f.timestamp for f in got] == [ts_of(g) for g in sorted(gids)]

    def test_duplicate_ts_yields_one_frame_each(self, hls_step, fake_decode):
        g = 17
        got = list(FrameTracker(TASK_ID, STEP_ID).find([ts_of(g), ts_of(g)], 4, 4))
        assert [f.timestamp for f in got] == [ts_of(g), ts_of(g)]

    @pytest.mark.parametrize("drift", [1e-6, -1e-6, 1e-3])
    def test_drifted_ts_raises(self, hls_step, fake_decode, drift):
        """ts 是帧的身份，不做近似匹配：配错帧比报错更坏。"""
        with pytest.raises(ValueError, match="未找到 ts="):
            list(FrameTracker(TASK_ID, STEP_ID).find([ts_of(17) + drift], 4, 4))

    def test_ts_outside_timeline_raises(self, hls_step, fake_decode):
        with pytest.raises(ValueError, match="未找到 ts="):
            list(FrameTracker(TASK_ID, STEP_ID).find([BASE_TS + 9999], 4, 4))

    def test_missing_step_raises_not_silently_empty(self, tmp_storage, fake_decode):
        """盘上一段都没有时也必须硬失败：静默返回空会让上游当成"这些帧没检测框"。"""
        with pytest.raises(ValueError, match="未找到 ts="):
            list(FrameTracker(999, 999).find([BASE_TS], 4, 4))

    def test_empty_input_yields_nothing(self, hls_step, fake_decode):
        assert list(FrameTracker(TASK_ID, STEP_ID).find([], 4, 4)) == []
