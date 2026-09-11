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

`ts_us` 是**截断**到微秒的墙钟（`int(ts * 1e6)`），不是四舍五入：读侧段级定位的
`bisect_right - 1` 建立在"段名 ts ≤ 段内首帧 ts"之上，进位会让它落到前一段（T2）。

依赖上界：stdlib only。
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, NamedTuple, Optional, Tuple

from app.storage import _root

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


class SegmentRef(NamedTuple):
    """一个段在其 step 内的身份：轨道 + 起始时刻（微秒截断）。

    **不带 `task_id` / `step_id`**（L3）：那两个是路由键、调用方手里本来就有；而 track
    与 ts_us 是从文件名里解出来的，不放进来就得让调用方自己再解一次。
    """

    track: str
    ts_us: int

    @property
    def ts_s(self) -> float:
        """段起始时刻（秒）。`ts_us` 已截断，回不到原始 float ts（T2）。"""
        return self.ts_us / 1_000_000.0


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

    Raises:
        ValueError: track 非法。
    """
    require_track(track)
    root = domain_dir(task_id, step_id)
    if not root.is_dir():
        return []

    refs = [
        ref
        for entry in root.iterdir()
        if entry.is_file() and (ref := parse_segment_name(entry.name)) is not None
        if ref.track == track
    ]
    refs.sort(key=lambda r: r.ts_us)
    return refs


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
    return domain_dir(task_id, step_id) / f"{require_track(track)}_init.mp4"


def init_name(track: str) -> str:
    """init 段的**文件名**——playlist 的 `EXT-X-MAP:URI` 写的是它，不是绝对路径。"""
    return f"{require_track(track)}_init.mp4"


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
