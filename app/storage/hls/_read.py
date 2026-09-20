"""读侧组合动作 —— 与 `_write.py` 对称：一次调用回答一个完整的读侧问题。

    list_segments(task, step, track)               有哪些段、各自多长（**段枚举的唯一入口**）
    list_segments_in_range(task, step, track, ...) 其中落在这个墙钟区间里的那些

两个同源、同返回类型（`List[Segment]`），后者只是前者加一次区间切片。

**「有哪些段」只由清单回答**，盘上有文件不算数：在途段与登记失败的段喂给下游是 hls.js
缓冲洞或 ffmpeg 静默截短（`docs/kb/DESIGN_SEGMENT_CONCAT.md` §5.3）。代价（未登记段的帧
离线反查不可达）与取舍见 `docs/update/20260919_VIDEO_TIMEBASE_SELECTION.md` §5.2。

**升序是返回值的契约**，下游的 `bisect_right - 1` 段级定位建立在它上面。

依赖上界：stdlib only（与 `_decode` 分模块正是为此：那边吃 numpy + ffmpeg 子进程）。
"""

from __future__ import annotations

import logging
from bisect import bisect_right
from typing import List, Optional

from . import _layout, _m3u8
from .types import Segment

logger = logging.getLogger(__name__)


def list_segments(task_id: int, step_id: int, track: str) -> List[Segment]:
    """该轨的段与各自的 EXTINF，按 `ts_us` 升序。清单缺失返回 `[]`。

    本域"有哪些段"的唯一出口——一行清单条目同时给出墙钟锚点（URI 里的 `ts_us`）与媒体长度
    （EXTINF），不需要跟文件系统 join。**不枚举目录**：盘上有文件而清单无条目的不是段。

    Raises:
        ValueError: track 非法。
    """
    _layout.require_track(track)

    out: List[Segment] = []
    for name, duration_s in _m3u8.entries(_layout.playlist_path(task_id, step_id, track)):
        ref = _layout.parse_segment_name(name)
        if ref is None or ref.track != track:
            # 手写进来的条目 / 别的轨的段名：不是本轨的合法段，跳过而不抛
            continue
        out.append(Segment(ref=ref, duration_s=duration_s))

    # 清单只追加，所以清单顺序本就是时序；仍显式排一次，因为**升序是返回值的契约**，
    # 下游的 `bisect_right - 1` 段级定位直接建立在它上面。
    #
    # 但排序**不能静悄悄地**：写侧的 `tfdt` 是按**清单顺序**累加 EXTINF 算出来的
    # （`_write` 的 ② adjust 步），媒体轴上的落点也按同样顺序推。两者只在"清单顺序 ==
    # ts 顺序"时一致，而那个前提恰好是 `_write` 反复警告的并发失效点（同 step 并发写 →
    # tfdt 碰撞）。真出现逆序时，这里排一下会让读侧算出的媒体偏移与文件里实际的 tfdt
    # 对不上——seek 到错误的帧，且不报错。所以要留一条日志，把静默错变成查得到的异常。
    if any(a.ref.ts_us > b.ref.ts_us for a, b in zip(out, out[1:])):
        logger.warning(
            "[storage.hls] 清单顺序与 ts 顺序不一致，已重排；tfdt 可能与媒体轴落点对不上: "
            "task_id=%s step_id=%s track=%s",
            task_id, step_id, track,
        )
        out.sort(key=lambda s: s.ref.ts_us)
    return out


def list_segments_in_range(
    task_id: int,
    step_id: int,
    track: str,
    *,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> List[Segment]:
    """`list_segments` 里落在墙钟区间 `[start_ts, end_ts]` 的那些，按 `ts_us` 升序。

    Args:
        task_id: 任务 id。
        step_id: 洗消步骤 id。
        track: 轨道名。
        start_ts / end_ts: 闭区间的墙钟秒，`None` 表示该侧不设限。

    Returns:
        段列表；没有段、或区间与任何段都不沾边时返回 `[]`。

    Raises:
        ValueError: track 非法。

    **判据是段的起始 ts，不是段的覆盖区间**：本函数的用途是给解码省 ffmpeg 调用次数，选宽
    一点只是多解一段（帧级裁剪会滤掉多余的帧），选窄了才是丢帧。要"与区间严格重叠"的调用方
    自己拿段尾口径（`ts_s + duration_s`）去筛。
    """
    segs = list_segments(task_id, step_id, track)
    if not segs:
        return []

    # 要找的是**包含** start_ts 的那一段，故 'right' - 1：'left' 取到的是 start_ts 之后的
    # 段，且段名 ts_us 是截断值，「start_ts 恰为该段首帧」时它同样会跳过该段 —— 无条件错。
    # 用 stdlib bisect 而非 np.searchsorted：ts_us < 2^53 时两者逐值等价。
    lo = (
        0
        if start_ts is None
        else max(0, bisect_right(segs, start_ts * 1e6, key=lambda s: s.ref.ts_us) - 1)
    )
    hi = (
        len(segs) - 1
        if end_ts is None
        else bisect_right(segs, end_ts * 1e6, key=lambda s: s.ref.ts_us) - 1
    )
    if lo > hi:  # end_ts 早于首段起点时 hi = -1，在此被拦下；刻意不 clamp 成 0
        return []

    return segs[lo : hi + 1]
