"""解码读循环：readinto 整帧直写。

钉三件事：短读跨边界拼帧正确、EOF 残帧丢弃、每帧是独立数组（下游持有的旧帧不被后续读覆盖）。
"""

from unittest.mock import MagicMock

import numpy as np

from app.services.stream.config import DecoderConfig
from app.services.stream.decoder import FFmpegDecoder


class _ShortReadPipe:
    """模拟管道：每次 readinto 最多给 `step` 字节（管道语义：读不满）。"""

    def __init__(self, data: bytes, step: int):
        self._data = data
        self._pos = 0
        self._step = step
        self.calls = 0

    def readinto(self, buf) -> int:
        self.calls += 1
        n = min(self._step, len(buf), len(self._data) - self._pos)
        buf[:n] = self._data[self._pos:self._pos + n]
        self._pos += n
        return n


def _decoder(w=4, h=2):
    cfg = DecoderConfig(default_width=w, default_height=h, default_fps=30)
    return FFmpegDecoder(manager=MagicMock(), task_id=1, stream_url="rtsp://x", decoder_config=cfg)


def _frames_bytes(n, size):
    return bytes(np.arange(n * size, dtype=np.uint8) % 251)


def test_short_reads_assemble_whole_frames():
    dec = _decoder()
    size = dec.frame_size  # 4*2*3 = 24
    raw = _frames_bytes(3, size)
    pipe = _ShortReadPipe(raw, step=7)  # 7 不整除 24：每帧都跨读边界

    frames = [dec._read_frame(pipe) for _ in range(3)]

    for i, f in enumerate(frames):
        assert f.shape == (2, 4, 3)
        assert f.tobytes() == raw[i * size:(i + 1) * size]
    assert dec._read_frame(pipe) is None


def test_eof_mid_frame_drops_partial():
    dec = _decoder()
    size = dec.frame_size
    pipe = _ShortReadPipe(_frames_bytes(1, size) + b"\x01" * (size // 2), step=size)

    assert dec._read_frame(pipe) is not None
    assert dec._read_frame(pipe) is None


def test_each_frame_is_a_fresh_array():
    """复用缓冲会让队列里已排的帧被下一帧静默覆盖。"""
    dec = _decoder()
    size = dec.frame_size
    raw = _frames_bytes(2, size)
    pipe = _ShortReadPipe(raw, step=size)

    first = dec._read_frame(pipe)
    snapshot = first.copy()
    second = dec._read_frame(pipe)

    assert not np.shares_memory(first, second)
    assert np.array_equal(first, snapshot)


def test_reader_loop_feeds_every_frame_then_stops_on_eof():
    dec = _decoder()
    size = dec.frame_size
    pipe = _ShortReadPipe(_frames_bytes(5, size), step=10)
    dec.proc = MagicMock(stdout=pipe)
    seen = []
    dec._process_frame = lambda std: seen.append(std.tobytes())

    dec._reader_loop()

    assert len(seen) == 5
