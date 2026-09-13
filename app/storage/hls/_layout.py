"""hls 域的定位、命名与枚举 —— 域内每条路径都从这里出来（规范 §7.3 L1-L5）。

    {root}/{task_id}/{step_id}/hls/
      {track}_segment_{ts_us}.mp4   段（fMP4 fragment）
      {track}_init.mp4              该轨的 init 段，首段产出、整条 playlist 复用
      {track}_playlist.m3u8         LIVE 形态播放列表
      raw_segment_{ts_us}.idx       raw 轨逐帧 ts sidecar（float64），仅离线反查用
      metadata.json                 段数 / 时长 / 首末 ts 统计，兼作 TTL 判据
      .stage_{track}_{ts_us}/       写入事务的暂存目录，commit 后即删（W7）

**身份键是 `SegmentRef(track, ts_us)`，不是散标量。** 读写两侧共用同一组定位函数
（L1）：写侧自己构造 ref，读侧从文件名 `parse_segment_name` 解出 ref，路径一律由 ref
重建——外部字符串从不进入路径拼接，path traversal 结构上不可能（L2）。
形状本身声明在 `types.py`（本域的资源容器集中一处）；本模块管的是**名字与位置**——
轨道白名单 `TRACKS` 与校验器 `require_track` 留在这里，它们是词汇表不是形状。

`ts_us` 是**截断**到微秒的墙钟（`int(ts * 1e6)`），不是四舍五入：读侧段级定位的
`bisect_right - 1` 建立在"段名 ts ≤ 段内首帧 ts"之上，进位会让它落到前一段（T2）。

依赖上界：stdlib only。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.storage import _root

from .types import SegmentRef

# 本域的域名 —— 全文件只出现这一次（样板见 `_root.py`）。
_DOMAIN = "hls"

# 合法轨道。两条轨各有各的 playlist 与 init，互不相干（见 `_m3u8` 的 EXT-X-MAP 说明）。
TRACKS: Tuple[str, ...] = ("raw", "processed")

# 产物文件名 —— 内容归本域自己持有，`_root` 对其零知识。
_SEGMENT_SUFFIX = ".mp4"
_SIDECAR_SUFFIX = ".idx"
_METADATA_NAME = "metadata.json"

# 段文件名格式：`{track}_segment_{ts_us}.mp4`。
# 它同时是 `parse_segment_name` 的校验器：斜杠、`..`、绝对路径、非法 track、非数字 ts
# 一律匹配不上 —— 这就是 L2 说的"外部输入先经 parse_* 转结构"的执行形态。
_SEGMENT_RE = re.compile(r"^(?P<track>raw|processed)_segment_(?P<ts_us>\d+)\.mp4$")

# init 文件名格式：`{track}_init.mp4`。同样是 `parse_init_name` 的校验器。
# **不能用 `endswith("init.mp4")` 顶替**：那会放行 `evil_init.mp4`、`../raw_init.mp4`，
# 于是"路径由结构重建"这条就断了，只能退回事后校验 resolve() 在不在根目录里（L2）。
_INIT_RE = re.compile(r"^(?P<track>raw|processed)_init\.mp4$")


def domain_dir(task_id: int, step_id: int, *, create: bool = False) -> Path:
    """本域在该 step 下的根目录 —— 域内所有路径函数都经它。

    `_root.py` 的样板里它叫 `_domain_root`（薄域是单文件，模块私有就够）。本域是子包，
    同包的 `_write.delete` 要整个删掉这个目录，故去掉前导下划线——它是**包内**公开、
    仍不出包（facade 不 re-export 它）。
    """
    return _root.path(task_id, step_id, _DOMAIN, create=create)


def require_track(track: str) -> str:
    """校验轨道名，返回原值；非法抛 `ValueError`。

    不给默认轨道：写错轨会静默写进另一条 playlist（两轨各自独立、都合法），产物齐全、
    没有任何一端报错，只是回放时 raw 里混进了渲染画面。
    """
    if track not in TRACKS:
        raise ValueError(f"Invalid track: {track!r}, expected one of {TRACKS}")
    return track


def ts_to_us(ts: float) -> int:
    """墙钟秒 → 文件名里的 `ts_us`。**截断**不是四舍五入，理由见模块 docstring。"""
    return int(ts * 1e6)


def segment_name(ref: SegmentRef) -> str:
    """`SegmentRef` → 段文件名（`parse_segment_name` 的逆运算，往返测试见 T1/T2）。"""
    return f"{require_track(ref.track)}_segment_{int(ref.ts_us)}{_SEGMENT_SUFFIX}"


def parse_segment_name(name: str) -> Optional[SegmentRef]:
    """段文件名 → `SegmentRef`；不是合法段名返回 `None`（不抛）。

    返回 `None` 而不是抛，是因为它的主用法是**在一堆文件里挑段**（枚举 `hls/` 目录时
    playlist、init、sidecar、stage 目录都会经过这里），"不是段"是常态不是错误。
    """
    m = _SEGMENT_RE.match(name)
    if m is None:
        return None
    return SegmentRef(track=m.group("track"), ts_us=int(m.group("ts_us")))


def list_segments(task_id: int, step_id: int, track: str) -> List[SegmentRef]:
    """该轨在这个 step 下的全部段，按 `ts_us` 升序。

    域目录不存在返回 `[]`（"还没写过" 不是错误）。**升序是返回值的契约**：读侧的段级
    定位（`bisect` / `searchsorted`）直接建立在它上面。

    枚举经 `parse_segment_name` 逐个过筛，这正是它 docstring 里说的主用法——playlist、
    init、sidecar、`.stage_` 目录都会流经这里，"不是段"是常态不是错误，故那边返回
    `None` 而不抛。

    ⚠ **本函数回答的是"盘上有哪些段文件"，不是"哪些段能播"**：在途段（mp4v 已落、转码
    或清单登记未完成）也在返回值里。要喂给播放器或 ffmpeg 的一律用 `_read.playable_segments`
    ——它按清单键集合过滤并顺带给出 EXTINF。拿本函数的结果去拼 VOD 清单会静默截短
    （`clip_builder` 的缺陷 #3 就是这么来的）。

    Raises:
        ValueError: track 非法。
    """
    require_track(track)
    return list_segments_by_track(task_id, step_id)[track]


def list_segments_by_track(task_id: int, step_id: int) -> Dict[str, List[SegmentRef]]:
    """该 step 下按轨道分组的段，**双轨只付一次 `iterdir`**；各轨内按 `ts_us` 升序。

    域目录不存在时返回各轨空列表（不是空 dict）——调用方可以直接按 track 取，不用先判键。

    要两轨的调用方（step 摘要要回答"这个 step 有哪些轨"）走这个；只要一轨的走
    `list_segments`，它就是本函数取一个键。分两次扫目录才是浪费，排序多排一条空列表不是。

    同样的「在途段也在返回值里」警告适用，见 `list_segments`。
    """
    by_track: Dict[str, List[SegmentRef]] = {t: [] for t in TRACKS}

    root = domain_dir(task_id, step_id)
    if not root.is_dir():
        return by_track

    for entry in root.iterdir():
        if not entry.is_file():
            continue
        ref = parse_segment_name(entry.name)
        if ref is not None:
            by_track[ref.track].append(ref)

    for refs in by_track.values():
        refs.sort(key=lambda r: r.ts_us)
    return by_track


def segment_path(task_id: int, step_id: int, ref: SegmentRef, *, create: bool = False) -> Path:
    """段文件路径。`create=True` 时确保 `hls/` 目录存在（写任何产物前用它）。"""
    return domain_dir(task_id, step_id, create=create) / segment_name(ref)


def sidecar_path(task_id: int, step_id: int, ref: SegmentRef) -> Path:
    """段的逐帧 ts sidecar 路径（同名换后缀）。

    **只有 raw 轨会产出它**（processed 是渲染结果、离线不消费），但本函数对任何 ref 都
    给得出路径——"该不该写"是写侧的事，定位函数只回答"在哪"。
    """
    return segment_path(task_id, step_id, ref).with_suffix(_SIDECAR_SUFFIX)


def init_path(task_id: int, step_id: int, track: str) -> Path:
    """该轨的 fMP4 init 段路径。

    **按 track 分开**：raw 与 processed 是两条独立 playlist、各有各的 EXT-X-MAP，共用
    一个文件名会变成"谁先转码谁定"，另一条轨就指向别人的 init。
    """
    return domain_dir(task_id, step_id) / init_name(track)


def init_name(track: str) -> str:
    """init 段的**文件名**——playlist 的 `EXT-X-MAP:URI` 写的是它，不是绝对路径。"""
    return f"{require_track(track)}_init.mp4"


def parse_init_name(name: str) -> Optional[str]:
    """init 文件名 → track；不是合法 init 名返回 `None`（`init_name` 的逆运算）。

    返回 `None` 而不抛，口径同 `parse_segment_name`：它的用法是**校验一个外部来的名字**
    （`/media/init/{token}` 解出的 filename）以及枚举时过筛，"不是 init"是常态不是错误。

    这是 L2 在 init 侧的执行形态：调用方拿 track 回头走 `init_path`，路径由结构重建，
    外部字符串从不进入拼接。
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
    """该段写入事务的暂存目录（W1/W7）。

    **与目标同卷**（就在 `hls/` 里面），`os.replace` 才是原子换名而不是跨卷复制；
    **目录名取「与产物同键」而不是随机 nonce**：每段最多留一份残留，重试自然复用同一个
    （入口 `rmtree` 一次即幂等），且目录名本身就说明"哪一份产物没写完"。

    前导点让它不像产物；它也匹配不上 `_SEGMENT_RE`，读侧枚举天然跳过。
    """
    return domain_dir(task_id, step_id) / f".stage_{require_track(ref.track)}_{int(ref.ts_us)}"
