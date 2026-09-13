"""hls 域的资源容器 —— 本域对外收发的数据形状，集中一处声明。

    SegmentRef        一个段在其 step 内的身份（写侧构造 / 读侧 parse 出来）
    PlayableSegment   已登记可播的段：身份 + 它在清单里声明的时长

两个都属本域读侧的**段容器**那一档（另一档是 `Frame`，全仓库的域货币，住在 `app.domain`）。

**只放对外契约**：函数内部传参用的四行 NamedTuple 放在用它的地方旁边。判据（换掉落盘格式
会不会跟着消失 / 是不是装配产物 / 消费方够不够两个）见
`docs/kb/DESIGN_STORAGE_LAYER.md` §2——`VodEntry` 与 `Step` 摘要按它出局。

**本模块是子包的底**：stdlib only，且不 import 同包任何模块（`_layout` / `_m3u8` / `_read` /
`_write` / `_decode` 都往这里取形状）。轨道白名单 `TRACKS` 与校验器 `require_track` 留在
`_layout`——那是词汇表不是形状，搬进来会让本模块反向依赖它。
"""

from __future__ import annotations

from typing import NamedTuple


class SegmentRef(NamedTuple):
    """一个段在其 step 内的身份：轨道 + 起始时刻（微秒截断）。

    **不带 `task_id` / `step_id`**：那两个是路由键、调用方手里本来就有。
    """

    track: str
    ts_us: int

    @property
    def ts_s(self) -> float:
        """段起始时刻（秒）。`ts_us` 已截断，回不到原始 float ts。"""
        return self.ts_us / 1_000_000.0


class PlayableSegment(NamedTuple):
    """一个已完成转码并登记的段：身份键 + 它在清单里声明的时长。

    两者**必须同源**：EXTINF 既是段时长的唯一真值，其键集合又是"这段能不能播"的判据。
    """

    ref: SegmentRef
    duration_s: float
