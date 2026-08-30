from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Optional, Iterator
import numpy as np

from app.domain.frame import Frame
from app.services.traceback.segment_finder import (
    SegmentFinder,
    SegmentRef,
    get_default_base_dir,
)


class Timeline:
    """时间线：ts → 段 → 段内帧号 → 像素。纯查询，零像素缓存。

    落盘形态：fMP4 按段落盘（raw_segment_{ts_us}.mp4），
    每段配一个同名 .idx sidecar（float64 时间戳数组，每帧一条）。
    解码走 `concat:raw_init.mp4|raw_segment_{ts}.mp4` + `select=between(n,k1,k2)`，
    不拼 m3u8、不用 -ss，故段内帧号与 sidecar 下标严格 1:1。

    对外只有一个 iter(start_ts, end_ts)。

    段级裁剪省 ffmpeg 调用次数，帧级裁剪不存无效像素。
    """

    def __init__(
        self,
        task_id: int,
        step_id: int,
        track: str = "raw",
        finder: Optional[SegmentFinder] = None,
        ffmpeg_bin: Optional[str] = None,
    ):
        self._finder = finder or SegmentFinder(get_default_base_dir())
        self._step_dir = self._finder.task_dir(task_id, step_id)
        self._segs = self._finder.list_segments(task_id, step_id, track)
        self._timestamps = np.array([seg.ts_us for seg in self._segs], dtype=np.float64)
        if ffmpeg_bin is None:
            from app.settings import settings
            ffmpeg_bin = settings.ffmpeg_path
        self._ffmpeg_bin = ffmpeg_bin

    def iter(
        self,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        width: int = 640,
        height: int = 480,
    ) -> Iterator[Frame]:
        """段级裁剪，返回一个时间范围内的所有 Frame。
        start_ts / end_ts 为 None 时，默认为时间轴首尾。
        
        注意：本方法不做任何 ts 匹配校验、不做单点筛选。
        该方法仅负责返回给定时间范围内的 Frame。
        """
        if not self._segs:
            return
        
        lo, hi = 0, len(self._segs) - 1
        if start_ts is not None:
            lo = np.searchsorted(self._timestamps, start_ts * 1e6, side='left')
            lo = max(0, min(len(self._segs), lo))
        if end_ts is not None:
            hi = np.searchsorted(self._timestamps, end_ts * 1e6, side='right') - 1
            hi = max(0, min(len(self._segs), hi))
        if lo > hi:
            return

        if start_ts is None:
            start_ts = self._timestamps[0] / 1e6
        if end_ts is None:
            end_ts = self._timestamps[-1] / 1e6

        for seg in self._segs[lo:hi+1]:
            yield from self._decode_segment(seg, start_ts, end_ts, width, height)

    def _decode_segment(
        self,
        seg: SegmentRef,
        start_ts: float,
        end_ts: float,
        width: int = 640,
        height: int = 480,
    ) -> Iterator[Frame]:
        """帧级裁剪，返回同一个段内的指定时间范围内的 Frame。"""
        sidecar = self._load_sidecar(seg)
        if len(sidecar) == 0:
            return
        k_start, k_end = 0, len(sidecar) - 1
        if sidecar[k_start] < start_ts:
            k_start = np.searchsorted(sidecar, start_ts, side='left')
            k_start = max(0, min(len(sidecar), k_start))
        if sidecar[k_end] > end_ts:
            k_end = np.searchsorted(sidecar, end_ts, side='right') - 1
            k_end = max(0, min(len(sidecar), k_end))
        if k_start > k_end:
            return
        
        yield from self._run_ffmpeg(seg, sidecar, k_start, k_end, width, height)

    def _run_ffmpeg(
        self, seg: SegmentRef, sidecar: np.ndarray, k_start: int, k_end: int, width: int, height: int
    ) -> Iterator[Frame]:
        """解出段内 [k_start, k_end] 闭区间的帧，yield Frame。"""
        frame_size = width * height * 3

        cmd = self._build_cmd(seg, k_start, k_end, width, height)
        with subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        ) as proc:
            try:
                for k in range(k_start, k_end + 1):
                    chunk = proc.stdout.read(frame_size)
                    if len(chunk) < frame_size:
                        raise RuntimeError(f"Incomplete frame from {seg.filename}")
                    pixel = np.frombuffer(chunk, np.uint8).reshape((height, width, 3))
                    yield Frame(timestamp=sidecar[k], frame=pixel)
            finally:
                proc.kill()
                proc.wait()

    def _load_sidecar(self, seg: SegmentRef) -> np.ndarray:
        idx_path = self._step_dir / Path(seg.filename).with_suffix(".idx")
        if not idx_path.exists():
            raise FileNotFoundError(f"Sidecar not found: {idx_path}")
        return np.fromfile(idx_path, dtype=np.float64)

    def _build_cmd(
        self, seg: SegmentRef, start: int, end: int, width: int, height: int
    ) -> list[str]:
        return [
            self._ffmpeg_bin,
            "-loglevel", "error", "-hide_banner",
            "-i", f"concat:{self._step_dir / 'raw_init.mp4'}|{self._step_dir / seg.filename}",
            "-vf", f"scale={width}:{height},select=between(n\\,{start}\\,{end})",
            "-vframes", str(end - start + 1),
            "-vsync", "0",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "pipe:1",
        ]


class FrameTracker:
    def __init__(self, task_id: int, step_id: int, track: str = "raw"):
        self._tl = Timeline(task_id, step_id, track)

    def find(self, timestamps: list[float], width: int, height: int) -> Iterator[Frame]:
        sorted_timestamps = sorted(timestamps)
        start_ts = float(sorted_timestamps[0])
        end_ts = float(sorted_timestamps[-1])

        idx = 0
        for frame in self._tl.iter(start_ts, end_ts, width, height):
            if abs(frame.timestamp - sorted_timestamps[idx]) <= 1e-4:
                yield frame
                idx += 1
        if idx < len(sorted_timestamps):
            raise ValueError(f"未找到 ts={sorted_timestamps[idx]} 对应帧")
