"""帧序列 → mp4v 视频文件，以及它的速率反推。

本模块是写入事务的 ① stage 步：把内存里的 `Frame` 序列编成一个**独立 mp4**（cv2 的
mp4v，带自己的 moov），交给 `_fmp4` 再转成 HLS 要的 fragment。

**`effective_fps` 是本域最要紧的一个数**：它同时决定三个值 —— VideoWriter 的编码帧率、
playlist 的 `EXTINF`、以及由 EXTINF 累加出来的 `tfdt`。三者必须同源，播放器才不会在段尾
停摆、总时长才不缩水（推导见 `docs/update/20260908_EXTINF_TFDT_CONTRACT.md`）。它算在
层内，正是为了让这三个值不必分三条路跨边界传（规范 §7.0 的推翻理由）。

依赖上界：`app.domain`（域货币 `Frame`，numpy 随它进来）+ stdlib。**cv2 走函数体内
import**（D1/D2）：模块级会让 `import app.storage.hls` 一律拉起 OpenCV（~250 ms），而
facade 的 re-export 会连带加载本模块。
"""

from __future__ import annotations

from pathlib import Path
from typing import Sequence

from app.domain.frame import Frame

# HLS 段的编码帧率正常由 `effective_fps` 从帧 ts 反推（完全自适应，不引用任何上游 fps）。
# 以下三个常量定义"无可测速率"的退化判定与兜底，全部具名、不散落在条件里：
#   _EFF_FPS_MIN / _EFF_FPS_MAX —— 反推值的合理带；落带外（乱序/重复 ts 致 span 异常）视为不可信。
#   _DEGENERATE_FALLBACK_FPS   —— 单帧段 / span<=0 / 带外 时的兜底。此时本就无时序信息，
#       取值与上游 fps 无关，只需给退化的单帧段一个合理 EXTINF；故用本地常量而非上游 raw_fps。
_EFF_FPS_MIN = 1.0
_EFF_FPS_MAX = 60.0
_DEGENERATE_FALLBACK_FPS = 15.0

# cv2 的 fourcc。mp4v 是中间态：它只是喂给 ffmpeg 的输入，最终产物是 `_fmp4` 转出的
# fragment（libx264）。这里不用 h264 是因为 cv2 的 openh264 后端在各平台可用性不一。
_FOURCC = "mp4v"


def effective_fps(frames: Sequence[Frame]) -> float:
    """由帧时间戳跨度反推有效编码 fps：`(N-1) / (ts_last - ts_first)`。

    VideoWriter 与 EXTINF 须用同一个返回值，回放才对齐墙钟。raw/processed 段一律走此
    自适应反推、不引用上游 fps。span<=0 / 单帧 / 反推值落在合理带 [1, 60] 外（重复或
    乱序时间戳致 span 异常）时——即无可测速率的退化段——回退 `_DEGENERATE_FALLBACK_FPS`。

    退化段是唯一不准的情形：它仍**自洽**（tfdt / mdhd / EXTINF 三者一致），播放不出洞，
    只是该段的墙钟↔媒体换算失真。
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

    **不能用首末帧 ts 跨度算**：那是墙钟量，比媒体时长少一个帧间隔，且断流时会把整个
    停顿算进段长。历史上用它推 EXTINF 导致过 hls.js 段尾停摆与总时长缩水。
    """
    return frame_count / fps


def write_mp4v(path: Path, frames: Sequence[Frame], fps: float) -> None:
    """把帧序列编码成 mp4v 文件（分辨率取首帧）。

    Raises:
        OSError: 编码器打不开或写入失败。cv2 的失败（`cv2.error`、VideoWriter 静默不
            open）在这里统一翻译成 `OSError` —— 本层不出领域异常（R3），而"编码器不可用"
            对调用方就是一次环境失败，与写不进盘同档（R6）。

    `isOpened()` 必须显式检查：cv2 打不开编码器时**不抛异常**，后续 `write()` 全部静默
    丢弃，留下一个 0 字节文件。不查就要等到转码那步才炸，且错因指向 ffmpeg。
    """
    # cv2 只在本函数用到，故在函数体内导入（规范 §2 通路 2，理由见模块 docstring）。
    import cv2

    height, width = frames[0].frame.shape[:2]
    fourcc = cv2.VideoWriter_fourcc(*_FOURCC)  # type: ignore[attr-defined]

    writer = None
    try:
        writer = cv2.VideoWriter(str(path), fourcc, fps, (width, height))
        if not writer.isOpened():
            raise OSError(f"cv2.VideoWriter 打不开编码器（fourcc={_FOURCC}）: {path}")
        for frame in frames:
            writer.write(frame.frame)
    except cv2.error as e:
        raise OSError(f"cv2 写 mp4v 段失败: {path}") from e
    finally:
        if writer is not None:
            writer.release()  # 异常路径也须释放原生编码器句柄
