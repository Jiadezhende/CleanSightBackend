"""媒体轴：一条轨的段序列展开成可定位的时间轴，以及墙钟↔媒体的双向换算。

    tl = hls.query_timeline(run, "raw")      # 读清单并展开（`_read.py`）
    tl.wall_ms_at(12_345)                    # 媒体刻度 → 绝对墙钟
    tl.media_ms_at(ts_ms)                    # 绝对墙钟 → 媒体刻度（告警标记落点用它）
    tl.select(a, b).wall_gaps()              # 这段区间里相邻段的墙钟空隙（不带阈值）

**媒体轴是压紧的墙钟**：断流那段时间在它上面宽度为零。所以 `首段墙钟 + 媒体刻度` 这个换算
只在从没断过流时成立，别在调用侧自己凑——换算要清单，只有本模块有。推导与三个同源表示见
`docs/update/20260919_VIDEO_TIMEBASE_SELECTION.md` §1、§5.3。

「多大的空隙算断流」不是盘上事实，阈值与判定在 `app/services/utils/media_timeline.py`。

依赖上界：stdlib + `.types`（本模块不碰盘）。
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Iterator, List, NamedTuple, Tuple

from .types import Segment


class PlacedSegment(NamedTuple):
    """一个段 + 它在媒体轴上的起点（ms）。"""

    seg: Segment
    media_start_ms: int

    @property
    def media_end_ms(self) -> int:
        return self.media_start_ms + int(round(self.seg.duration_s * 1000))

    @property
    def wall_start_ms(self) -> int:
        return self.seg.ref.ts_ms

    @property
    def wall_end_ms(self) -> int:
        """段尾墙钟 = 段起点 + EXTINF。

        **不能用"下一段起点"代替**：那会把断流停顿算进段长，而这个差值正是判空洞要的量。
        """
        return self.wall_start_ms + int(round(self.seg.duration_s * 1000))

    def wall_ms_at(self, media_ms: int) -> int:
        """段内某个媒体刻度对应的绝对墙钟（线性插值，误差 ≤ 一帧）。"""
        return self.wall_start_ms + (media_ms - self.media_start_ms)

    def media_ms_at(self, wall_ms: int) -> int:
        """段内某个墙钟对应的媒体刻度（上式的逆）。"""
        return self.media_start_ms + (wall_ms - self.wall_start_ms)


class MediaTimeline:
    """一条轨的段序列在媒体轴上的展开。由 `hls.query_timeline` 构造；`MediaTimeline([])` 是空轨。

    刻度一律**相对整条轨的原点**，`select()` 取子集后也不重新归零——否则调用方手上那个
    绝对刻度就没法直接相减。
    """

    def __init__(self, placed: List[PlacedSegment]):
        self._placed = placed

    # -------- 容器 --------

    def __len__(self) -> int:
        return len(self._placed)

    def __bool__(self) -> bool:
        return bool(self._placed)

    def __iter__(self) -> Iterator[PlacedSegment]:
        return iter(self._placed)

    @property
    def duration_ms(self) -> int:
        """媒体轴总长（Σ EXTINF）—— 与 `<video>.duration` 同源。空轨为 0。"""
        return self._placed[-1].media_end_ms if self._placed else 0

    def select(self, start_media_ms: int, end_media_ms: int) -> "MediaTimeline":
        """与 `[start_media_ms, end_media_ms)` 相交的那些段，坐标不变。"""
        return MediaTimeline(
            [
                p
                for p in self._placed
                # 标准区间相交：a_start < b_end AND a_end > b_start
                if p.media_start_ms < end_media_ms and p.media_end_ms > start_media_ms
            ]
        )

    # -------- 换算 --------

    def media_offset_ms(self, media_ms: int) -> int:
        """相对本窗口首段起点的偏移 —— 交给 ffmpeg `-ss` 的就是这个数。

        取子集时 ffmpeg 会把首段的 `tfdt` 归一到 0（实测：绝对落点 100s 的子集照样得到
        `start_time=0`），所以偏移是纯减法，不需要知道整条轨的原点在哪。
        """
        return media_ms - self._placed[0].media_start_ms

    def wall_ms_at(self, media_ms: int) -> int:
        """媒体刻度 → 绝对墙钟。超出范围时贴到最近的一端。"""
        for p in self._placed:
            if media_ms < p.media_end_ms:
                return p.wall_ms_at(max(media_ms, p.media_start_ms))
        last = self._placed[-1]
        return last.wall_ms_at(last.media_end_ms)

    def media_ms_at(self, wall_ms: int) -> int:
        """绝对墙钟 → 媒体刻度。超出范围时贴到最近的一端。

        **落进空洞的墙钟吸附到下一段的段首**：那段时间在媒体轴上不存在，宽度为零，没有
        "对应刻度"可言；吸到下一段段首是唯一不撒谎的选择（不会声称某一帧拍于空洞之中）。
        """
        if not self._placed:
            return 0
        starts = [p.wall_start_ms for p in self._placed]
        i = bisect_right(starts, wall_ms) - 1
        if i < 0:                                   # 早于首段
            return 0
        p = self._placed[i]
        if wall_ms >= p.wall_end_ms:                # 落在该段之后：空洞里，或整条轨之后
            nxt = self._placed[i + 1] if i + 1 < len(self._placed) else None
            return nxt.media_start_ms if nxt is not None else self.duration_ms
        return p.media_ms_at(wall_ms)

    # -------- 空隙 --------

    def wall_gaps(self) -> Iterator[Tuple[PlacedSegment, PlacedSegment, int]]:
        """每对相邻段的墙钟空隙 `(前段, 后段, 下一段起点 − (本段起点 + EXTINF) ms)`，不带阈值。

        **判据必须留在墙钟轴上**：媒体轴是压紧的，相邻段在它上面永远首尾相接。空隙可正可负
        （帧间隔抖动），哪些算断流由调用方按阈值筛。
        """
        for cur, nxt in zip(self._placed, self._placed[1:]):
            yield cur, nxt, nxt.wall_start_ms - cur.wall_end_ms
