"""hls 域的资源容器 —— 本域对外收发的数据形状，集中一处声明。

    SegmentRef   一个段在其 step 内的身份（写侧构造 / 读侧 parse 出来）
    Segment      一个段：身份 + 它在清单里声明的时长
    HlsSpan      一个 run 若干轨的段在墙钟上的跨度（`query_span` 的返回）

前两个属本域读侧的**段容器**那一档（另一档是 `Frame`，全仓库的域货币，住在 `app.types`）；
`HlsSpan` 是段容器的汇总，消费方是 routers 的 task / traceback / lab 三处。

**只放对外契约**：函数内部传参用的四行 NamedTuple 放在用它的地方旁边。判据（换掉落盘格式
会不会跟着消失 / 是不是装配产物 / 消费方够不够两个）见
`docs/kb/DESIGN_STORAGE_LAYER.md` §2——`VodEntry` 与 `Step` 摘要按它出局。

**本模块是子包的底**：stdlib only，且不 import 同包任何模块（`_layout` / `_m3u8` / `_read` /
`_write` / `_decode` 都往这里取形状）。轨道白名单 `TRACKS` 与校验器 `require_track` 留在
`_layout`——那是词汇表不是形状，搬进来会让本模块反向依赖它。
"""

from __future__ import annotations

from typing import NamedTuple, Tuple


class SegmentRef(NamedTuple):
    """一个段在其 step 内的身份：轨道 + 起始时刻（epoch 毫秒，向下取整）。

    **不带 `task_id` / `step_id`**：那两个是路由键、调用方手里本来就有。
    """

    track: str
    ts_ms: int

    @property
    def ts_s(self) -> float:
        """段起始时刻（秒）。`ts_ms` 已向下取整，回不到原始 float ts。"""
        return self.ts_ms / 1000.0


class Segment(NamedTuple):
    """一个段：身份键 + 它在清单里声明的时长。

    两者**必须同源**——都从清单的同一行条目来：URI 给身份，EXTINF 给时长。**没有"不在清单
    里的段"这回事**：盘上有文件而清单无条目的，是在途产物或登记失败的残留，不是段。
    """

    ref: SegmentRef
    duration_s: float


class HlsSpan(NamedTuple):
    """一个 run 若干轨的段在墙钟上的跨度（各轨取并集），单位 epoch 毫秒。

    `end_ms` = max(段起点 + round(EXTINF))，不是末段起点——后者漏掉末段自身长度。
    """

    tracks: Tuple[str, ...]   # 有段的那些轨，保持入参顺序
    start_ms: int             # 最早段起点
    last_start_ms: int        # 最晚段起点
    end_ms: int               # 最晚段尾
