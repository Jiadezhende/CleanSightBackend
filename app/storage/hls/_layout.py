"""hls 域的定位与命名 —— 域内每条路径都从这里出来。

    {root}/{task_id}/{step_id}/hls/
      {track}_segment_{ts_us}.mp4   段（fMP4 fragment）      {track}_init.mp4   该轨 init
      {track}_playlist.m3u8         LIVE 清单               raw_segment_{ts_us}.idx  逐帧 ts
      metadata.json                 统计，兼作 TTL 判据      .stage_{track}_{ts_us}/  写入暂存

**身份键是 `SegmentRef(track, ts_us)`**：外部字符串一律先 `parse_segment_name` 解成 ref，
路径由 ref 重建，绝不进字符串拼接。

`ts_us` 是**截断**到微秒（`int(ts * 1e6)`）不是四舍五入：读侧 `bisect_right - 1` 建立在
「段名 ts ≤ 段内首帧 ts」之上，进位会让它落到前一段。

**本模块不枚举目录**——「有哪些段」只由清单回答（`_read.list_segments`），
理由见 `docs/update/20260919_VIDEO_TIMEBASE_SELECTION.md` §5.2。

依赖上界：stdlib only。规范见 `docs/kb/DESIGN_STORAGE_LAYER.md` §3。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Optional, Tuple

from app.storage import _root

from .types import SegmentRef

# 本域的域名 —— 全文件只出现这一次。
_DOMAIN = "hls"

# 合法轨道。两条轨各有各的 playlist 与 init，互不相干。
TRACKS: Tuple[str, ...] = ("raw", "processed")

# 产物文件名 —— 内容归本域自己持有，`_root` 对其零知识。
_SEGMENT_SUFFIX = ".mp4"
_SIDECAR_SUFFIX = ".idx"
_METADATA_NAME = "metadata.json"

# 段名 / init 名格式，同时是 parse_* 的校验器：斜杠、`..`、绝对路径、非法 track、非数字 ts
# 一律匹配不上。**不能用 `endswith("init.mp4")` 顶替**——那会放行 `evil_init.mp4`。
_SEGMENT_RE = re.compile(r"^(?P<track>raw|processed)_segment_(?P<ts_us>\d+)\.mp4$")
_INIT_RE = re.compile(r"^(?P<track>raw|processed)_init\.mp4$")


def domain_dir(task_id: int, step_id: int, *, create: bool = False) -> Path:
    """本域在该 step 下的根目录 —— 域内所有路径函数都经它。

    无前导下划线是因为同包的 `_write.delete` 要用它：**包内**公开，仍不出包。
    """
    return _root.path(task_id, step_id, _DOMAIN, create=create)


def require_track(track: str) -> str:
    """校验轨道名，返回原值；非法抛 `ValueError`。

    不给默认轨道：写错轨会静默写进另一条 playlist（两轨都合法），产物齐全、无一端报错，
    只是回放时 raw 里混进了渲染画面。
    """
    if track not in TRACKS:
        raise ValueError(f"Invalid track: {track!r}, expected one of {TRACKS}")
    return track


def ts_to_us(ts: float) -> int:
    """墙钟秒 → 文件名里的 `ts_us`。**截断**不是四舍五入，理由见模块 docstring。"""
    return int(ts * 1e6)


def segment_name(ref: SegmentRef) -> str:
    """`SegmentRef` → 段文件名（`parse_segment_name` 的逆运算）。"""
    return f"{require_track(ref.track)}_segment_{int(ref.ts_us)}{_SEGMENT_SUFFIX}"


def parse_segment_name(name: str) -> Optional[SegmentRef]:
    """段文件名 → `SegmentRef`；不是合法段名返回 `None`（不抛——枚举目录时"不是段"是常态）。"""
    m = _SEGMENT_RE.match(name)
    if m is None:
        return None
    return SegmentRef(track=m.group("track"), ts_us=int(m.group("ts_us")))


def segment_path(task_id: int, step_id: int, ref: SegmentRef, *, create: bool = False) -> Path:
    """段文件路径。`create=True` 时确保 `hls/` 目录存在（写任何产物前用它）。"""
    return domain_dir(task_id, step_id, create=create) / segment_name(ref)


def sidecar_path(task_id: int, step_id: int, ref: SegmentRef) -> Path:
    """段的逐帧 ts sidecar 路径（同名换后缀）。

    **只有 raw 轨会产出它**，但本函数对任何 ref 都给得出路径——"该不该写"是写侧的事。
    """
    return segment_path(task_id, step_id, ref).with_suffix(_SIDECAR_SUFFIX)


def init_path(task_id: int, step_id: int, track: str) -> Path:
    """该轨的 fMP4 init 段路径。**按 track 分开**——两轨各有各的 EXT-X-MAP，共用一个文件名
    会让后写的那条轨指向别人的 init。
    """
    return domain_dir(task_id, step_id) / init_name(track)


def init_name(track: str) -> str:
    """init 段的**文件名**——playlist 的 `EXT-X-MAP:URI` 写的是它，不是绝对路径。"""
    return f"{require_track(track)}_init.mp4"


def parse_init_name(name: str) -> Optional[str]:
    """init 文件名 → track；不是合法 init 名返回 `None`（`init_name` 的逆运算）。

    用法是校验外部来的名字（`/media/init/{token}` 解出的 filename）：拿到 track 后回头走
    `init_path`，路径由结构重建，外部字符串从不进入拼接。
    """
    m = _INIT_RE.match(name)
    if m is None:
        return None
    return m.group("track")


def playlist_path(task_id: int, step_id: int, track: str) -> Path:
    """该轨的 LIVE playlist 路径。"""
    return domain_dir(task_id, step_id) / f"{require_track(track)}_playlist.m3u8"


def metadata_path(task_id: int, step_id: int) -> Path:
    """本域的统计文件路径（两轨共用一份）。"""
    return domain_dir(task_id, step_id) / _METADATA_NAME


def stage_dir(task_id: int, step_id: int, ref: SegmentRef) -> Path:
    """该段写入事务的暂存目录。

    **与目标同卷**（就在 `hls/` 里面），`os.replace` 才是原子换名而不是跨卷复制；目录名
    **与产物同键**（不是随机 nonce），故每段最多留一份残留、重试自然复用。它匹配不上
    `_SEGMENT_RE`，读侧枚举天然跳过。
    """
    return domain_dir(task_id, step_id) / f".stage_{require_track(ref.track)}_{int(ref.ts_us)}"
