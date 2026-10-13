"""段的编码速率反推与媒体时长（写入事务 ① stage 步的两个数）。

**`effective_fps` 是本域最要紧的一个数**：它同时决定 ffmpeg 的 `-framerate`（`_fmp4.encode`）、
playlist 的 `EXTINF`、以及由 EXTINF 累加出来的 `tfdt`，三者必须同源，否则播放器段尾停摆或
总时长缩水（推导见 `docs/update/20260908_EXTINF_TFDT_CONTRACT.md`）。

依赖上界：`app.types`（域货币 `Frame`）+ stdlib。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from app.types.frame import Frame

# 编码帧率由 `effective_fps` 从帧 ts 反推，不引用任何上游 fps。以下定义"无可测速率"的
# 退化判定与兜底：反推值落在 [MIN, MAX] 外（乱序/重复 ts 致 span 异常）视为不可信。
_EFF_FPS_MIN = 1.0
_EFF_FPS_MAX = 60.0
_DEGENERATE_FALLBACK_FPS = 15.0


def effective_fps(frames: Sequence[Frame]) -> float:
    """由帧时间戳跨度反推有效编码 fps：`(N-1) / (ts_last - ts_first)`。

    ffmpeg `-framerate` 与 EXTINF 须用**同一个返回值**，回放才对齐墙钟。span<=0 / 单帧 / 反推值落在
    合理带 [1, 60] 外时回退 `_DEGENERATE_FALLBACK_FPS`——退化段仍自洽（tfdt / mdhd / EXTINF
    三者一致、播放不出洞），只是墙钟↔媒体换算失真。
    """
    if len(frames) > 1:
        span = frames[-1].timestamp - frames[0].timestamp
        if span > 0:
            eff_fps = (len(frames) - 1) / span
            if _EFF_FPS_MIN <= eff_fps <= _EFF_FPS_MAX:
                return eff_fps
    return _DEGENERATE_FALLBACK_FPS


def media_duration(frame_count: int, fps: float) -> float:
    """该段的媒体时长 = `帧数 / 编码帧率`。EXTINF 与 tfdt 都从这个值来。

    **不能用首末帧 ts 跨度算**：那是墙钟量，会把断流停顿算进段长，表现是 hls.js 段尾停摆。
    """
    return frame_count / fps
