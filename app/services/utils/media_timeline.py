"""媒体轴：段序列展开成可定位的时间轴，以及墙钟↔媒体的双向换算。

    tl = MediaTimeline.load(task_id, step_id, "raw")
    tl.wall_ms_at(12_345)        # 媒体刻度 → 绝对墙钟
    tl.media_ms_at(ts_ms)        # 绝对墙钟 → 媒体刻度（告警标记落点用它）
    tl.select(a, b).first_gap()  # 这段区间里有没有断流

**媒体轴是压紧的墙钟**：断流那段时间在它上面宽度为零。所以
`首段墙钟 + 媒体刻度` 这个换算只在从没断过流时成立，别在调用侧自己凑——
换算要清单，只有本模块有。推导与三个同源表示见
`docs/update/20260919_VIDEO_TIMEBASE_SELECTION.md` §1、§5.3。

`load()` 的累加**依赖 EXTINF 落盘精度是 ms 整数倍**（写侧 `_m3u8.entry()` 的 `:.3f`）；
改了那个精度这里要换成整数累加，否则段间出现 ±1ms 错位、`select()` 边界漏段。

依赖上界：`app.storage` + stdlib。
"""

from __future__ import annotations

from bisect import bisect_right
from typing import Iterator, List, NamedTuple, Optional, Tuple

from app.storage import hls
from app.storage.hls import Segment

# 判为"真实录制停顿"的相邻段间隙下限。
#
# **保守护栏，不是精确判据。** 下界：正常段边界的残差是帧间隔量级——`EXTINF = N/eff_fps` 比
# 段的墙钟跨度多整一个帧间隔，正好抵掉跨边界那一次，于是残差是「本次帧间隔 − 段内平均帧间
# 隔」，典型 ≈0、尾部几十 ms。上界：能触发重连的断流 = decoder 进程退出 → 健康检查发现
# （`check_interval` 1s 内）→ respawn → RTSP 重连，量级是秒。
#
# 0.5s 落在这一到两个数量级的空当里，且**不随段长或 fps 变**——被它替换掉的「相邻段 ts 中位
# 差 + 容差」判据正是要跟着调的那种。精确判据要等空洞轮（那时 health_monitor 会声明断点真值）。
GAP_THRESHOLD_MS = 500


class PlacedSegment(NamedTuple):
    """一个段 + 它在媒体轴上的起点（ms）。"""

    seg: Segment
    media_start_ms: int

    @property
    def media_end_ms(self) -> int:
        return self.media_start_ms + int(round(self.seg.duration_s * 1000))

    @property
    def wall_start_ms(self) -> int:
        return self.seg.ref.ts_us // 1000

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
    """一条轨的段序列在媒体轴上的展开。

    刻度一律**相对整条轨的原点**，`select()` 取子集后也不重新归零——否则调用方手上那个
    绝对刻度就没法直接相减。
    """

    def __init__(self, placed: List[PlacedSegment]):
        self._placed = placed

    @classmethod
    def load(cls, task_id: int, step_id: int, track: str) -> "MediaTimeline":
        """读清单并展开。清单顺序即时序，累加 EXTINF 即落点。

        累加用 float 秒、只在出口取整到 ms：逐段取整再累加会让误差随段数线性累积
        （180 段最坏 90ms）。

        **"无损"依赖一个跨模块前提：EXTINF 落盘精度是 ms 的整数倍。** 写侧
        `app/storage/hls/_m3u8.py` 的 `entry()` 用 `:.3f`，所以每个 EXTINF 都是整毫秒，
        float64 累加几百个这样的值在 ms 精度下不产生错位。若哪天把落盘精度提到六位小数，
        这里就会出现 ±1ms 的段间错位——表现是 `select()` 在边界漏段、`media_offset_ms`
        变负（`clip_builder` 有 clamp 兜底，只会多裁 1ms）。真要改精度，这里得改成整数累加。
        """
        placed: List[PlacedSegment] = []
        cursor_s = 0.0
        for seg in hls.list_segments(task_id, step_id, track):
            placed.append(
                PlacedSegment(seg=seg, media_start_ms=int(round(cursor_s * 1000)))
            )
            cursor_s += seg.duration_s
        return cls(placed)

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

    # -------- 空洞 --------

    def first_gap(self) -> Optional[Tuple[PlacedSegment, PlacedSegment, int]]:
        """第一处真实录制停顿，`(前段, 后段, 空洞 ms)`；没有则 `None`。

            gap = 下一段起点 − (本段起点 + EXTINF)

        两项都是真值：起点是文件名里的实测墙钟，EXTINF 是清单里的段媒体时长。fps 漂移天然
        被吸收——漂移同时压低 `eff_fps` 与段内帧数，`EXTINF = N/eff_fps` 跟着变长。

        **判据必须留在墙钟轴上。** 媒体轴是压紧的，相邻段在它上面永远首尾相接，断没断过流
        都一样，空洞宽度恒为零。
        """
        for cur, nxt, gap_ms in self._gaps():
            return cur, nxt, gap_ms
        return None

    def total_gap_ms(self) -> int:
        """本轨累计断流时长（所有超阈值空隙之和）。没有空洞则 0。

        **这是唯一正确的算法，别用 `墙钟跨度 − Σ EXTINF` 去凑。** 那两个量不同尺：
        `/timeline` 的 `duration_ms` 取**双轨并集**的墙钟跨度，而 Σ EXTINF 是**单轨**的，
        差值里混着"两轨起止不对齐"这一项——推理起步晚于取流时 processed 首段本就晚于 raw
        首段，于是零断流的 step 也会算出一个假空洞。
        """
        return sum(gap_ms for _, _, gap_ms in self._gaps())

    def _gaps(self) -> Iterator[Tuple[PlacedSegment, PlacedSegment, int]]:
        for cur, nxt in zip(self._placed, self._placed[1:]):
            gap_ms = nxt.wall_start_ms - cur.wall_end_ms
            if gap_ms > GAP_THRESHOLD_MS:
                yield cur, nxt, gap_ms
