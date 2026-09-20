"""mp4v → HLS-ready fMP4 fragment，以及 fragment 内 `tfdt` 的改写。

    source.mp4  ──ffmpeg(hls muxer, fmp4)──▶  init.mp4 + fragment_0.mp4
                                                      │
                                       hex-patch ─────┘  moof/traf/tfdt

两件事都不是可选的：普通 MP4（moov+mdat 整体）被 hls.js 当段播会 fragParsingError；而每个段
由独立 ffmpeg 进程转码、输入自身从 PTS=0 起，不补 tfdt 偏移则所有 fragment 落点都是 0，
播到第一段末尾就不前进。**tfdt 只能改字节**（ffmpeg 的 `-output_ts_offset` /
`-itsoffset+-copyts` / `-muxdelay` 在 HLS muxer + fmp4 下全部无效），box 结构固定、size 不变，
是纯 metadata 改写。背景与实测见 `docs/kb/DESIGN_HLS_TIMELINE.md`。

失败语义：ffmpeg 缺失 / 超时 / 非零退出 / 没产出预期文件一律**原样抛**，由调用方删掉整个
stage、不 rename、不登记。本模块不做降级保留——留下一个 mp4v 冒充 fragment 是静默失败。

依赖上界：stdlib only（`app.settings` 只在函数体内 import）。
"""

from __future__ import annotations

import logging
import struct
import subprocess
from pathlib import Path
from typing import Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

# fMP4 媒体时间基（mdhd.timescale），**显式 pin 给 ffmpeg**：不指定时它按段自选，而 init 只由
# 首段生成、被整条 playlist 复用，逐段 timescale 不同会让后续 fragment 的 tick 被按首段尺度
# 解读，误差是乘性的（实测单段 2.4s 空洞）。取 90000 的理由与实测见
# `docs/kb/DESIGN_HLS_TIMELINE.md`。
TIMESCALE = 90000

# 转码超时（秒）。**必须有超时**：调用侧是单写者（一条 SerialTaskQueue 一个消费线程），一个
# 卡住的 ffmpeg 堵的不是自己那一段，是所有 task 的录制。实测单段 ~260 ms、1080p 满段 1–3 s，
# 15 s 已是 5–10× 余量。
_TIMEOUT_S = 15

# stage 目录内的固定文件名。它们**不是产物名**——产物名由 `_layout` 决定，commit 时才 rename
# 过去；固定命名让转码步不必知道自己在为哪个段服务。
_SOURCE_NAME = "source.mp4"
_INIT_NAME = "init.mp4"
_FRAGMENT_NAME = "fragment_0.mp4"
# `-hls_segment_filename` 必须含 %d 模板（即便只有 1 段），配 `-start_number 0` 让产物固定为
# fragment_0.mp4。
_FRAGMENT_TEMPLATE = "fragment_%d.mp4"
_PLAYLIST_NAME = "index.m3u8"

# ISO/IEC 14496-12 box 容器集合：递归扫描 box 树时只下钻这些类型，
# 其余 box（含 tfdt、mdhd）按 leaf 处理。
_BOX_CONTAINERS = frozenset({b"moov", b"trak", b"mdia", b"moof", b"traf", b"mvex"})


def source_path(stage: Path) -> Path:
    """stage 目录里那份待转码的 mp4v 的位置（由 `_encode.write_mp4v` 写入）。"""
    return stage / _SOURCE_NAME


def seconds_to_ticks(seconds: float) -> int:
    """媒体轴秒 → tfdt 的 tick。timescale 是 pin 死的常量，与 init 声明的必然一致。"""
    return int(round(seconds * TIMESCALE))


# ── 转码 ─────────────────────────────────────────────────────────────────────────


def transcode(stage: Path) -> Tuple[Path, Path]:
    """把 `stage/source.mp4` 转成 fMP4，返回 `(fragment, init)` 两个 stage 内路径。

    Raises:
        FileNotFoundError: 机器上没有 ffmpeg（运行时依赖，import 期不要求）。
        subprocess.TimeoutExpired: 超过 `_TIMEOUT_S`。
        subprocess.CalledProcessError: ffmpeg 非零退出（stderr 挂在异常上）。
        RuntimeError: ffmpeg 报成功但没产出 fragment 或 init。

    两条 ffmpeg 写法上的硬约束，改了会静默或跨平台炸（背景见
    `docs/kb/DESIGN_HLS_TIMELINE.md`）：

    - **子进程 `cwd=stage`，所有输出只传 basename**：8.x 与 4.x 对 `-hls_fmp4_init_filename`
      的路径解析行为正好相反，绝对路径在其中一边必然 ENOENT。
    - **`-hls_segment_options video_track_timescale=`** 是唯一能 pin 住 mdhd.timescale 的写法
      （透传给内层 mp4 muxer）；直接给 hls muxer 传 `-video_track_timescale` 会被静默忽略。

    ffmpeg 自己在 stage 里写的那份 `index.m3u8` 本层不用（playlist 由 `_m3u8` 按整条 step 的
    口径维护），随 stage 目录一起删。
    """
    from app.settings import settings

    cmd = [
        settings.ffmpeg_path,
        "-y",
        "-loglevel", "error",
        "-i", _SOURCE_NAME,
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-an",
        # tfdt 偏移不在这里靠 -output_ts_offset 实现（ffmpeg 8.x HLS muxer + fmp4 在
        # -start_number 0 下会清零 tfdt）—— 改成转码完 hex-patch，见 `patch_tfdt`。
        "-hls_segment_type", "fmp4",
        "-hls_segment_options", f"video_track_timescale={TIMESCALE}",
        "-hls_fmp4_init_filename", _INIT_NAME,
        "-hls_segment_filename", _FRAGMENT_TEMPLATE,
        "-start_number", "0",
        "-hls_time", "99999",
        "-hls_list_size", "0",
        "-hls_flags", "temp_file",
        "-f", "hls",
        _PLAYLIST_NAME,
    ]
    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=_TIMEOUT_S,
        cwd=str(stage),
    )
    if result.returncode != 0:
        raise subprocess.CalledProcessError(
            result.returncode, cmd, output=result.stdout, stderr=result.stderr
        )

    fragment = stage / _FRAGMENT_NAME
    init = stage / _INIT_NAME
    missing = [p.name for p in (fragment, init) if not p.exists()]
    if missing:
        raise RuntimeError(f"ffmpeg 退出码 0 但未产出 {missing}: {stage}")
    return fragment, init


# ── ISO BMFF box 遍历与 tfdt 改写 ────────────────────────────────────────────────


def _iter_boxes(data: bytes, start: int, end: int) -> Iterator[Tuple[bytes, int, int]]:
    """遍历 [start, end) 范围内的 box，逐个 yield `(type, body_start, body_end)`。

    遇到 `_BOX_CONTAINERS` 中的容器盒返回容器本身的位置，调用方自行决定是否再次下钻。
    截断或 size 异常时停止（坏文件不该让解析器越界读）。
    """
    i = start
    while i + 8 <= end:
        size = struct.unpack(">I", data[i : i + 4])[0]
        typ = data[i + 4 : i + 8]
        header = 8
        if size == 1:
            if i + 16 > end:
                return
            size = struct.unpack(">Q", data[i + 8 : i + 16])[0]
            header = 16
        elif size == 0:
            size = end - i  # extends to container end
        if size < header or i + size > end:
            return
        yield typ, i + header, i + size
        i += size


def _find_box_path(
    data: bytes, start: int, end: int, path: Tuple[bytes, ...]
) -> Optional[Tuple[int, int]]:
    """按 box 类型路径定位最里层 box，返回其 body 范围；找不到返回 `None`。

    path 形如 `(b"moof", b"traf", b"tfdt")`。中间节点必须是容器；最后一段是 leaf。
    """
    if not path:
        return start, end
    head, *rest = path
    for typ, body_start, body_end in _iter_boxes(data, start, end):
        if typ != head:
            continue
        if not rest:
            return body_start, body_end
        if typ in _BOX_CONTAINERS:
            found = _find_box_path(data, body_start, body_end, tuple(rest))
            if found is not None:
                return found
    return None


def patch_tfdt(fragment: Path, base_media_decode_time: int) -> bool:
    """把 fragment 的 `moof/traf/tfdt.baseMediaDecodeTime` 改写成指定 tick 值。

    Returns:
        是否改写成功。**找不到 box / 版本异常返回 False 而不抛** —— 它意味着"这个
        fragment 不是我们以为的形状"，是内容事实不是环境故障；调用方（`_insert`）据此
        决定作废还是放行。读失败与写失败仍按 `OSError` 抛出。

    约定 tfdt 为 version 1（64-bit），ffmpeg HLS muxer 在 fmp4 输出时一律按 v1 写；
    v0 分支只是保底，且 32 位放不下时拒绝截断（截断出来是个错位的落点，比不改更坏）。
    """
    data = bytearray(fragment.read_bytes())

    moof = _find_box_path(bytes(data), 0, len(data), (b"moof",))
    if moof is None:
        logger.warning("[storage.hls] moof not found in %s — tfdt 未改写", fragment)
        return False
    traf = _find_box_path(bytes(data), moof[0], moof[1], (b"traf",))
    if traf is None:
        logger.warning("[storage.hls] traf not found in %s — tfdt 未改写", fragment)
        return False
    tfdt = _find_box_path(bytes(data), traf[0], traf[1], (b"tfdt",))
    if tfdt is None:
        logger.warning("[storage.hls] tfdt not found in %s — tfdt 未改写", fragment)
        return False

    body_start, body_end = tfdt
    if body_end - body_start < 4:
        return False
    version = data[body_start]
    if version == 1:
        if body_end - body_start < 4 + 8:
            return False
        struct.pack_into(">Q", data, body_start + 4, base_media_decode_time)
    else:
        if body_end - body_start < 4 + 4:
            return False
        if base_media_decode_time > 0xFFFFFFFF:
            logger.warning(
                "[storage.hls] tfdt v0 装不下 %d（>2^32）: %s",
                base_media_decode_time, fragment,
            )
            return False
        struct.pack_into(">I", data, body_start + 4, base_media_decode_time)

    fragment.write_bytes(bytes(data))
    return True
