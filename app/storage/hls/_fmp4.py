"""mp4v → HLS-ready fMP4 fragment，以及 fragment 内 `tfdt` 的改写。

    source.mp4  ──ffmpeg(hls muxer, fmp4)──▶  init.mp4 + fragment_0.mp4
                                                      │
                                       hex-patch ─────┘  moof/traf/tfdt

**为什么非转不可**：普通 MP4（moov+mdat 整体）无法被 hls.js 当段播放，会 fragParsingError。
`-hls_segment_type fmp4` 让 ffmpeg 产出 init segment（ftyp+moov）+ fragment（ftyp+moof+mdat），
才符合 HLS 协议。

**为什么 tfdt 要自己改**：每个段由独立 ffmpeg 进程转码、输入 mp4v 自身从 PTS=0 开始，不补
偏移则所有 fragment 的 tfdt 都是 0，hls.js 播到第一段末尾就不前进。而 ffmpeg 8.x 的 HLS
muxer 在 `-start_number 0` + fmp4 下会强制清零 tfdt，`-output_ts_offset` /
`-itsoffset+-copyts` / `-muxdelay` 全部无效——只能转码完直接改字节。fmp4 的 box 结构固定、
size 不变，是纯 metadata 改写。

失败语义（W4）：ffmpeg 缺失 / 超时 / 非零退出 / 没产出预期文件，一律**原样抛**，由
`_insert` 删掉整个 stage、不 rename、不登记。本模块不做任何降级保留——留下一个 mp4v 冒充
fragment，playlist 里就多一条喂给 demuxer 会出垃圾的条目，而那是静默失败。

依赖上界：stdlib only（`app.settings` 只在函数体内 import，规范 §2）。
"""

from __future__ import annotations

import logging
import struct
import subprocess
from pathlib import Path
from typing import Iterator, Optional, Tuple

logger = logging.getLogger(__name__)

# fMP4 媒体时间基（mdhd.timescale，即 1 秒切成多少 tick），显式 pin 给 ffmpeg。
#
# 必须 pin 的理由：不指定时 ffmpeg 按 `fps 有理数的约分分子 × 2^k`（k 取到 ≥10000）自选
# timescale，而 init.mp4 只由首段生成、被整条 playlist 复用（EXT-X-MAP 声明的就是它的
# 时间基）。逐段 eff_fps 不同 → 逐段 timescale 不同 → 后续 fragment 的 tick 被按首段尺度
# 解读，误差是乘性的：实测 15fps 定 init、14.37fps 段（其自选 timescale=11496）→ 声明
# 10.02s 却被读成 7.60s，单段 2.4s 空洞，hls.js 段尾停摆。
# 注意该自选值对 fps 极不连续——15.0→15360 但 14.37→11496（分子 1437 约不动），fps 抖 4%
# 可致 timescale 差 25%，故「fps 波动不大就没事」不成立。
#
# 取 90000：MPEG-TS/RTP 标准视频时钟，能整除 30/25/24/20/15/12/10 等常见帧率（每帧分别
# 3000/3600/3750/4500/6000/7500/9000 tick，无余数）；非整除帧率下 ffmpeg 按绝对 PTS 取整、
# 增量差分得出，误差有界 ≤ 半 tick（5.6μs）且不累积。
# pin 之后 timescale 与编码 fps 彻底解耦，逐段 eff_fps 才是合法的速率表达。
TIMESCALE = 90000

# 转码超时（秒）。D3′ ①：层内起的外部工具必须有超时，否则一个卡住的 ffmpeg 会永久占住
# 调用方的线程。
#
# 取 15 而不是"给足余量"的大数：调用侧是**单写者**（一条 SerialTaskQueue 一个消费线程），
# 一个卡住的 ffmpeg 堵的不是自己那一段，是**所有 task 的录制**。实测单段 ~260 ms，
# 1080p 满段量级 1–3 s，15 s 已是 5–10× 余量；再大只是把「卡住」变成「卡更久」。
_TIMEOUT_S = 15

# stage 目录内的固定文件名。它们**不是产物名**——产物名由 `_layout` 决定，commit 时才
# rename 过去。固定命名让转码步不必知道自己在为哪个段服务。
_SOURCE_NAME = "source.mp4"
_INIT_NAME = "init.mp4"
_FRAGMENT_NAME = "fragment_0.mp4"
# ffmpeg HLS muxer 要求 `-hls_segment_filename` 必须含 %d 模板（即便只有 1 段），否则报
# "Invalid segment filename template"。pin `-start_number 0` 让产物固定为 fragment_0.mp4。
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
        FileNotFoundError: 机器上没有 ffmpeg（D5：那是运行时依赖，import 期不要求）。
        subprocess.TimeoutExpired: 超过 `_TIMEOUT_S`。
        subprocess.CalledProcessError: ffmpeg 非零退出（stderr 挂在异常上）。
        RuntimeError: ffmpeg 报成功但没产出 fragment 或 init。

    **路径策略：子进程 cwd=stage，所有输出全传 basename。** 历史踩坑——ffmpeg 8.x
    (Windows) 把 `-hls_fmp4_init_filename` 的 basename 解析到进程 cwd、传绝对路径才对；
    ffmpeg 4.x (Ubuntu 22.04) 把绝对路径**当相对路径**拼到 playlist 目录前，得到
    `/dir/foo/dir/foo/init.mp4` 这种荒诞路径 → ENOENT。两版行为正好相反，唯一兼容写法
    就是 cwd + basename：两边都拼到 stage 里。

    `-hls_segment_options video_track_timescale=` 是**唯一**能 pin 住 mdhd.timescale 的
    写法：它透传给内层 mp4 muxer；直接给 hls muxer 传 `-video_track_timescale` 会被静默
    忽略（理由见 `TIMESCALE` 注释）。

    ffmpeg 自己也会在 stage 里写一份 `index.m3u8`，本层不用它——playlist 由 `_m3u8` 按
    整条 step 的口径维护，转码只出字节。它随 stage 目录一起删。
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
