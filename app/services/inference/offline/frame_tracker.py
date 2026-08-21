from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Optional, Iterator
from collections import OrderedDict

import numpy as np

from app.domain.frame import Frame

# fMP4 媒体时间基，与 hls_strategy.py 的 _HLS_TIMESCALE 必须同值同源。
# tick = (ts - first_ts) * _HLS_TIMESCALE — 这里算的 tick 与 _update_timeline 写入
# .timeline.idx 的 tick 是同一个量，故 timeline 索引能正确反查 frame_num。
# 详见 hls_strategy.py 该常量注释（为什么 pin 90000、为什么不随 fps 变）。
_HLS_TIMESCALE = 90000


class Timeline:
    """时间线：维护 ts → frame_num 的映射关系。

    读 hls_strategy._update_timeline 产出的 .timeline.idx（二进制 numpy structured array，
    dtype=[("tick", uint64)]，每帧一条 tick 记录，按 raw 段落盘顺序追加）。
    建立 tick → frame_num（0-based，全局递增）的反查字典，frame_num 即该帧在整条
    raw_playlist 中的全局序号。查询时把 ts 转成 tick 直接查表。
    """

    def __init__(self, task_dir: Path):
        self._first_ts = self._get_first_ts(task_dir)
        data = self._load_from_file(task_dir)
        self._frame_nums = {tick: i for i, tick in enumerate(data["tick"])}

    def _get_first_ts(self, task_dir: Path) -> float:
        metadata_path = task_dir / "metadata.json"
        with open(metadata_path, "r") as f:
            return json.load(f)["raw_segments"]["first_timestamp"]

    def _load_from_file(self, task_dir: Path) -> np.ndarray:
        return np.fromfile(
            task_dir / ".timeline.idx",
            dtype=np.dtype([("tick", np.uint64)]),
        )

    def frame_num_at(self, ts: float) -> int:
        # tick 公式与 hls_strategy._update_timeline 完全一致，保证查表命中。
        tick = int((ts - self._first_ts) * _HLS_TIMESCALE)
        return self._frame_nums[tick]


class FrameCache:
    """帧缓存：维护 frame_num → frame 的映射关系。

    用 ffmpeg 从 raw_playlist.m3u8 按 frame_num 范围提取帧（select=between），
    按 block_size 分块缓存（LRU），避免逐帧 seek 的 ffmpeg 启动开销。

    """
    def __init__(
        self,
        task_dir: Path,
        capacity: int = 10,
        block_size: int = 600,
        ffmpeg_bin: Optional[str] = None,
    ):
        """初始化帧缓存

        task_dir: 任务目录，包含 raw_playlist.m3u8 和 metadata.json。
        capacity: 缓存块最大数量。
        block_size: 每个缓存块的帧数。
        """
        self._capacity = capacity
        self._block_size = block_size
        self._cache: OrderedDict[int, np.ndarray] = OrderedDict()

        self._task_dir = task_dir
        self._timeline = Timeline(task_dir)

        if ffmpeg_bin is None:
            from app.settings import settings
            ffmpeg_bin = settings.ffmpeg_path
        self._ffmpeg = ffmpeg_bin

    def frame_at(self, ts: float, width: int, height: int) -> np.ndarray:
        """根据 ts 提取 frame_num 对应的 frame。
        缓存命中时直接返回，否则从 raw_playlist.m3u8 提取并缓存。

        ts: 时间戳，单位秒。
        width: 目标帧宽度。
        height: 目标帧高度。

        返回：
        np.ndarray: 目标帧数据，形状为 (height, width, 3)。
        """
        frame_num = self._timeline.frame_num_at(ts)
        idx, offset = divmod(frame_num, self._block_size)
        block = self._cache.get(idx)
        if block is None:
            block = self._load_block(idx, width, height)
        self._update_cache(idx, block)
        return block[offset]

    def _update_cache(self, block_id: int, block: np.ndarray):
        # LRU cache
        if block_id in self._cache:
            self._cache.move_to_end(block_id)
        else:
            if len(self._cache) >= self._capacity:
                self._cache.popitem(last=False)
        self._cache[block_id] = block

    def _load_block(self, block_id: int, width: int, height: int) -> np.ndarray:
        """
        加载第 block_id 个数据块，每个块包含 block_size 帧。
        ffmpeg between 左右均为闭区间。
        """
        start = block_id * self._block_size
        end = start + self._block_size - 1
        return self._extract_frames(start, end, width, height)

    def _extract_frames(self, start: int, end: int, width: int, height: int) -> np.ndarray:
        # 用 ffmpeg select=between(n) 从 raw_playlist 按全局帧号范围提取，
        # 输出 rawvideo bgr24 到 pipe。-vsync 0 禁用丢帧/重复帧，保证 count 精确。
        m3u8 = self._task_dir / "raw_playlist.m3u8"
        count = end - start + 1
        frame_size = width * height * 3

        cmd = [
            self._ffmpeg,
            "-i", m3u8.as_posix(),
            "-vf", f"select=between(n\\,{start}\\,{end})",
            "-vframes", str(count),
            "-vsync", "0",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "pipe:1",
        ]

        pipe = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=frame_size * 8,
        )

        frames = []
        try:
            while len(frames) < count:
                chunk = pipe.stdout.read(frame_size)
                if not chunk:
                    break
                if len(chunk) != frame_size:
                    raise RuntimeError("Incomplete frame from FFmpeg")
                frames.append(chunk)
        except Exception as e:
            pipe.kill()
            raise RuntimeError(f"FFmpeg decode error: {e}") from e

        stderr = pipe.stderr.read()
        ret = pipe.wait()
        if ret != 0:
            raise RuntimeError(f"FFmpeg failed (code {ret}): {stderr.decode(errors='ignore')}")

        # 若读取到视频末尾，会比 count 少一些帧，此处不做严格校验。
        raw = b"".join(frames)
        return np.frombuffer(raw, np.uint8).reshape(-1, height, width, 3)

class FrameTracker:
    """离线推理帧回看：给定 features.jsonl 内的记录，逐帧从 HLS 落盘段提取原始帧。
    记录示例：
    {"ts": 1785982791.9397182, "features": {...}, "frame_width": 640, "frame_height": 480}
    
    Pipeline: ts → Timeline.frame_num_at → FrameCache.frame_at（LRU + ffmpeg select）→ Frame。
    """
    def __init__(self, task_dir: Path, capacity: int = 10, block_size: int = 600):
        self._cache = FrameCache(task_dir, capacity, block_size)

    def find(self, records: list[dict]) -> Iterator[Frame]:
        for record in records:
            ts = float(record["ts"])
            width, height = int(record["frame_width"]), int(record["frame_height"])
            frame = self._cache.frame_at(ts=ts, width=width, height=height)
            yield Frame(timestamp=ts, frame=frame)
