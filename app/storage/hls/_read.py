"""读侧组合动作 —— 与 `_write.py` 对称：一次调用回答一个完整的读侧问题。

    playable_segments(task, step, track)               哪些段能播，各自多长
    select_segments(task, step, track, start, end)     哪些段落在这个墙钟区间里

两个都产出**段容器**，那是本域读侧两种产出之一（另一种是 `Frame`，归 `_decode`）。

**只出事实，不出装配**：「把段列表变成一份 VOD 清单」在
`app/services/utils/vod_playlist.py`；「这个 step 有哪些轨、段跨多久」由调用方拿
`_layout.list_segments_by_track` 自己统计。

**「能播」是清单说了算，不是盘上有文件说了算**。段文件名一出现就对读侧可见，但它此刻可能是
在途段（旧平铺布局有 ~260ms 窗口），也可能是登记失败的段（段已就位、`_m3u8.append` 抛了
`OSError`，永久停在"有文件没条目"）。两种喂给播放器都是 hls.js 缓冲洞或静默截短，判据只有
一个——**在不在清单的键集合里**。

依赖上界：stdlib only（与 `_decode` 分模块正是为此：那边吃 numpy + ffmpeg 子进程）。
"""

from __future__ import annotations

from bisect import bisect_right
from typing import List, Optional

from . import _layout, _m3u8
from .types import PlayableSegment, SegmentRef


def playable_segments(task_id: int, step_id: int, track: str) -> List[PlayableSegment]:
    """该轨**已完成转码并登记**的段，按 `ts_us` 升序。域目录或清单缺失返回 `[]`。

    要喂给播放器或 ffmpeg 的一律走本函数，不要用 `_layout.list_segments`（那个含在途段）。

    Raises:
        ValueError: track 非法。
    """
    refs = _layout.list_segments(task_id, step_id, track)
    if not refs:
        return []

    durs = _m3u8.durations(_layout.playlist_path(task_id, step_id, track))
    if not durs:
        return []

    out: List[PlayableSegment] = []
    for ref in refs:
        dur = durs.get(_layout.segment_name(ref))
        if dur is not None:
            out.append(PlayableSegment(ref=ref, duration_s=dur))
    return out


def select_segments(
    task_id: int,
    step_id: int,
    track: str,
    *,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> List[SegmentRef]:
    """落在墙钟区间 `[start_ts, end_ts]` 里的段，按 `ts_us` 升序。

    Args:
        task_id: 任务 id。
        step_id: 洗消步骤 id。
        track: 轨道名。
        start_ts / end_ts: 闭区间的墙钟秒，`None` 表示该侧不设限。

    Returns:
        段身份键列表；没有段、或区间与任何段都不沾边时返回 `[]`。

    Raises:
        ValueError: track 非法。

    **判据是段的起始 ts，不是段的覆盖区间**：本函数的用途是给解码省 ffmpeg 调用次数，选宽
    一点只是多解一段（帧级裁剪会滤掉多余的帧），选窄了才是丢帧。要"与区间严格重叠"的调用方
    自己拿段尾口径去筛。

    ⚠ 与 `_layout.list_segments` 同样**不滤在途段**。
    """
    refs = _layout.list_segments(task_id, step_id, track)
    if not refs:
        return []

    # 要找的是**包含** start_ts 的那一段，故 'right' - 1：'left' 取到的是 start_ts 之后的
    # 段，且段名 ts_us 是截断值，「start_ts 恰为该段首帧」时它同样会跳过该段 —— 无条件错。
    # 用 stdlib bisect 而非 np.searchsorted：ts_us < 2^53 时两者逐值等价。
    lo = (
        0
        if start_ts is None
        else max(0, bisect_right(refs, start_ts * 1e6, key=lambda r: r.ts_us) - 1)
    )
    hi = (
        len(refs) - 1
        if end_ts is None
        else bisect_right(refs, end_ts * 1e6, key=lambda r: r.ts_us) - 1
    )
    if lo > hi:  # end_ts 早于首段起点时 hi = -1，在此被拦下；刻意不 clamp 成 0
        return []

    return refs[lo : hi + 1]


