"""读侧组合动作 —— 与 `_write.py` 对称：一次调用回答一个完整的读侧问题。

    playable_segments(task, step, track)               哪些段能播，各自多长
    select_segments(task, step, track, start, end)     哪些段落在这个墙钟区间里

两个都产出**段容器**，那是本域读侧两种产出之一（另一种是 `Frame`，归 `_decode`）。

**只出事实，不出装配**。「把段列表变成一份 VOD 清单」是给播放器或 ffmpeg 消费的装配
产物，不是段的元数据——整件事在 `app/services/utils/vod_playlist.py`，本模块只回答"哪些
段能播、各自多长"，怎么拼、每条 URI 长什么样是服务层的判断。域里曾有过 `render_vod` /
`build_vod_playlist` / `VodPlaylist.with_uris`，**都已移出或删除**。

「这个 step 有哪些轨、段跨多久」同样不在这里：要完整摘要的只有 `routers/task.py` 一个
消费方，按准入判据 2「< 2 不进」；它拿 `_layout.list_segments_by_track` 自己统计。

**为什么单独一个模块**：`_layout` 是最底层的纯定位（只 import `_root`），被 `_write` 与
`_decode` 依赖；让它反向 import `_m3u8` 会把"读清单内容"混进"布局与命名"。而与
`_decode.py` 分开，是因为那边吃 numpy + ffmpeg 子进程，本模块 stdlib only——合并会让
两者的导入预算互相拖累。段级定位因此用 stdlib `bisect` 而不是 `np.searchsorted`
（`select_segments` 的实现注释里有等价性的账）。

## 「能播」是清单说了算，不是盘上有文件说了算

段文件名一出现就对读侧可见，但它此刻可能还是：

    在途段        新写侧已消除（产物在 .stage_*/ 里造好才换名），**旧平铺布局仍有 ~260ms 窗口**
    登记失败的段   段已就位、`_m3u8.append` 抛了 OSError —— 永久停在"有文件没条目"

两种都不能喂给播放器：fragment 的媒体时长与清单声明对不上，表现是 hls.js 缓冲洞或
ffmpeg 拼出来的视频静默截短。判据只有一个——**在不在清单的键集合里**。

依赖上界：stdlib only。
"""

from __future__ import annotations

from bisect import bisect_right
from typing import List, Optional

from . import _layout, _m3u8
from .types import PlayableSegment, SegmentRef


def playable_segments(task_id: int, step_id: int, track: str) -> List[PlayableSegment]:
    """该轨**已完成转码并登记**的段，按 `ts_us` 升序。

    域目录或清单缺失返回 `[]`（"还没写过"不是错误）。升序继承自 `_layout.list_segments`。

    要喂给播放器或 ffmpeg 的一律走本函数，不要用 `_layout.list_segments`——那个回答的是
    "盘上有哪些段文件"，含在途段，拿去拼清单会静默截短。

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

    **判据是段的起始 ts，不是段的覆盖区间**——段尾要么去清单查 EXTINF（多一次读），要么
    靠相邻段 ts 差估算（不准），而本函数的用途是**给解码省 ffmpeg 调用次数**：选宽一点
    只是多解一段，帧级裁剪会把多余的帧滤掉；选窄了才是丢帧。要"与区间严格重叠"的调用方
    （送标裁剪）自己拿段尾口径去筛，那是策略不是定位。

    返回 `SegmentRef` 而不是 `PlayableSegment`：消费方是解码，不关心 EXTINF。「按区间取
    **可播**段」按准入判据 2 暂不建。

    ⚠ 与 `_layout.list_segments` 同样**不滤在途段**，见那边的警告。
    """
    refs = _layout.list_segments(task_id, step_id, track)
    if not refs:
        return []

    # 要找的是**包含** start_ts 的那一段，故 'right' - 1：'left' 取到的是 start_ts
    # **之后**的段。且段文件名的 ts_us 是截断值（`_layout.ts_to_us`），「start_ts 恰为
    # 该段首帧」时 start_ts*1e6 > ts_us，'left' 同样会跳过该段 —— 即不存在「大部分
    # 情况下对」，是无条件错。
    #
    # 用 stdlib bisect 而非 np.searchsorted（本模块 stdlib only，见模块 docstring）：
    # 两者在此逐值等价 —— ts_us < 2^53 时 float64 精确表示整数，int 与 float 的比较在
    # Python 里也是精确的，故切点相同。
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


