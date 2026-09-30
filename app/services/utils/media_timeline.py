"""断流判定：媒体轴上相邻段的墙钟空隙超过阈值即算一次真实录制停顿。

    tl = hls.query_timeline(run, "raw")
    first_gap(tl.select(a, b))   # 这段区间里有没有断流 → (前段, 后段, 空洞 ms) / None
    total_gap_ms(tl)             # 本轨累计断流时长

媒体轴本身（段落点、墙钟↔媒体换算、不带阈值的 `wall_gaps()`）在 `app/storage/hls/_timeline.py`；
本模块只加阈值这一条判断。

依赖上界：`app.storage` + stdlib。
"""

from __future__ import annotations

from typing import Iterator, Optional, Tuple

from app.storage.hls import MediaTimeline, PlacedSegment

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


def _gaps(tl: MediaTimeline) -> Iterator[Tuple[PlacedSegment, PlacedSegment, int]]:
    for cur, nxt, gap_ms in tl.wall_gaps():
        if gap_ms > GAP_THRESHOLD_MS:
            yield cur, nxt, gap_ms


def first_gap(tl: MediaTimeline) -> Optional[Tuple[PlacedSegment, PlacedSegment, int]]:
    """第一处真实录制停顿，`(前段, 后段, 空洞 ms)`；没有则 `None`。

        gap = 下一段起点 − (本段起点 + EXTINF)

    两项都是真值：起点是文件名里的实测墙钟，EXTINF 是清单里的段媒体时长。fps 漂移天然
    被吸收——漂移同时压低 `eff_fps` 与段内帧数，`EXTINF = N/eff_fps` 跟着变长。
    """
    for cur, nxt, gap_ms in _gaps(tl):
        return cur, nxt, gap_ms
    return None


def total_gap_ms(tl: MediaTimeline) -> int:
    """本轨累计断流时长（所有超阈值空隙之和）。没有空洞则 0。

    **别用 `墙钟跨度 − Σ EXTINF` 去凑。** `/timeline` 的 `duration_ms` 取**双轨并集**的墙钟
    跨度，Σ EXTINF 是**单轨**的，差值里混着「两轨起止不对齐」这一项——推理起步晚于取流时
    processed 首段本就晚于 raw 首段，零断流的 step 也会算出一个假空洞。
    """
    return sum(gap_ms for _, _, gap_ms in _gaps(tl))
