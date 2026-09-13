"""hls 域的资源容器 —— 本域对外收发的数据形状，集中一处声明。

    SegmentRef        一个段在其 step 内的身份（写侧构造 / 读侧 parse 出来）
    PlayableSegment   已登记可播的段：身份 + 它在清单里声明的时长

两个都属读侧契约里的**段容器**那一档（另一档是 `Frame`，它是全仓库的域货币、住在
`app.domain`）。本域读侧只有这两种产出，第三种形状要进来先过下面那两道。

**`VodEntry` 曾在这里，已移出**（→ `app/services/utils/vod_playlist.py`）。它不是段的
元数据，是**给播放器或 ffmpeg 消费的装配产物**里的一行：换掉落盘格式，`SegmentRef` 与
`PlayableSegment` 会跟着消失，而"一份 VOD 清单由若干 (URI, 时长) 组成"照旧成立——方向
正好相反，说明它不属于本域。此前把它留在这里的理由是「m3u8 是一种容器格式的形状」，
那条按**产物 vs 元数据**的判据作废。

**「step 摘要」刻意不在这里**（曾建过 `Step` 又撤掉）：`step` 在本仓库已是业务实体
（`clean_task.current_step`、`clean_alarm.step_id/step_name`），域内再定义一个同名类型是
同名不同义——判据同 `models.py` vs `types.py` 那条。更要紧的是**需要完整摘要的只有
`routers/task.py` 一个消费方**（`routers/lab.py` 只问"这个 step 有没有 raw 段"），
按准入判据 2「< 2 不进」不成立。调用方拿 `_layout.list_segments_by_track` 自己统计即可，
那是三行的事，不值一个类型。

**为什么这些不进 `app/domain/`**：`app.domain` 要挡的是「服务之间为了互相拿契约而互相
依赖，顺带把重依赖传染出去」。而调用方 import 本层不带任何重依赖（本域的重依赖集合为空，
cv2 / ffmpeg 都在函数体内或子进程里），所以本域的资源容器就地声明即可。
判据也不在「跨不跨服务」上——把落盘换成别的东西，`Frame` 还在，而
`SegmentRef(track, ts_us)` 就是盘上那个文件名解出来的、`PlayableSegment` 的"已登记"这个
状态来自清单键集合，两者都会一起消失。让 `app.domain` 认识它们等于让最上游的契约层持有
最下游的文件名格式知识。

**文件名即依赖上界**（规范 §1）：本模块 **stdlib only**，且不 import 同包任何模块——
它是本子包的底，`_layout` / `_m3u8` / `_read` / `_write` / `_decode` 都往这里取形状。
轨道白名单 `TRACKS` 与校验器 `require_track` 刻意留在 `_layout`：那是「布局与命名」的
词汇表，不是形状；且把校验器搬进来会让本模块反向依赖 `_layout`，底就不是底了。

**只放对外契约**。纯粹的打包形状（比如某个函数内部传参用的四行 NamedTuple）照旧放在用
它的地方旁边——同 `recording/_SegmentJob` 的立场。
"""

from __future__ import annotations

from typing import NamedTuple


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


class PlayableSegment(NamedTuple):
    """一个已完成转码并登记的段：身份键 + 它在清单里声明的时长。

    两者绑在一起出，是因为它们**必须同源**：EXTINF 既是段时长的唯一真值，其键集合又是
    "这段能不能播"的判据。分两次调用去取就会多扫一次清单，还给了两者对不上的机会。
    """

    ref: SegmentRef
    duration_s: float
