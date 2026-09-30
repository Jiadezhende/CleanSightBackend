"""VOD 形态 m3u8 —— 不落盘，每次请求现生成，带 `EXT-X-ENDLIST`。

    entries = [VodEntry(uri_of(s.ref), s.duration_s) for s in hls.list_segments(...)]
    text = render_vod(entries, map_uri=uri_of_init)

## 为什么它不在 `app/storage/hls/` 域里

hls 域的读侧只出**两种产出**：段容器（`SegmentRef` / `Segment`，回答"有哪些段、
在哪、能不能播、多长"）和 `Frame`（把段还原成帧）。VOD 清单两样都不是——它是**给播放器
或 ffmpeg 消费的装配产物**，不是段的元数据。段在盘上是什么样，与"把它们拼成一份谁能消费
的清单"是两件事：换掉落盘格式，段容器与 Frame 都还在，而这份文本会整个消失。

界线画在**产物**上而不是画在 URI 上（曾短暂地画在 URI 上：域出文本格式、URI 归调用方）。
那条线的问题是域里留着一个只差一个字段就变成「供人塞 token 的钩子」的函数，而域对 token
本该零认知。

## 为什么收 `VodEntry` 而不收 `Segment`

**时长从哪来是调用方的判断**，本模块只统一骨架。收 `VodEntry` 而不是 `Segment`
让本模块保持 stdlib only、不依赖 `app.storage`——这是它能同时服务浏览器与 ffmpeg 两类
消费者的前提。

> ⚠ 这里曾写着「`lab/clip_builder` 的 EXTINF 取相邻段 ts 差而非清单 EXTINF，因为 `-ss` 的
> seek 基准是墙钟」。**实测推翻了它**：ffmpeg 的时间轴完全来自 fragment 自己的 `tfdt` +
> sample duration，改写清单 EXTINF 是空操作（`docs/update/20260919_VIDEO_TIMEBASE_SELECTION.md`
> §4）。现在三个消费方**一律**传清单 EXTINF 真值，别再照旧说法把 ts 差捡回来。

依赖上界：stdlib only。
"""

from __future__ import annotations

import math
from typing import NamedTuple, Sequence


class VodEntry(NamedTuple):
    """VOD 清单里的一个段条目：**已经定好的** URI + 该段时长。

    `uri` 由调用方给，本模块不生成也不改写——它可能是裸文件名（喂 ffmpeg，相对清单位置
    解析）、绝对路径，或 token 化的 HTTP URL。**"段 URI 长什么样"是表示层与鉴权的事**。

    `duration_s` 一律取清单的 EXTINF（`hls.list_segments` 给），三个消费方都是。
    **不能用相邻段 ts 差重推**——那是墙钟量，断流时会把整个停顿算进段长。
    """

    uri: str
    duration_s: float


def render_vod(entries: Sequence[VodEntry], *, map_uri: str) -> str:
    """有序条目 + init 引用 → VOD 形态清单全文（含 `EXT-X-ENDLIST`）。

    **本函数只管 m3u8 的文本格式**：URI 长什么样（裸文件名 / 绝对路径 / token 化 URL）
    由调用方定好了再交进来。

    与写侧落盘的 LIVE 清单（`hls` 域内的 `_m3u8.header` + `entry`）是两种形态，不能互相
    替代：**缺 `ENDLIST` 会让 ffmpeg 当直播流只读 live edge，前面的段全丢**；播放器则会
    一直轮询清单等新段。

    `map_uri` 必填、无默认（R4）：漏传该是 `TypeError`。fMP4 fragment 没有 `EXT-X-MAP`
    就解不出 codec init，播放器与 ffmpeg 都会失败——而那是运行时才炸。

    `TARGETDURATION` 取 `ceil(最长 EXTINF)` 下限 1 —— RFC 8216 要求它 **≥** 每段 EXTINF，
    用 `round` 会在段长 10.4 s 时写出 10 而违规。

    Raises:
        ValueError: `entries` 为空。空清单的 `TARGETDURATION` 无从计算，且"一个段都没有
            的 VOD"不是"没事发生"；"这个 step 还没有可播段"由 `hls.list_segments`
            返回空列表来表达，怎么映射成错误是调用方的判断。
    """
    if not entries:
        raise ValueError("Cannot render a VOD playlist with no entries")

    target_duration = max(math.ceil(max(e.duration_s for e in entries)), 1)
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        f'#EXT-X-MAP:URI="{map_uri}"',
    ]
    for e in entries:
        lines.append(f"#EXTINF:{e.duration_s:.3f},")
        lines.append(e.uri)
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
