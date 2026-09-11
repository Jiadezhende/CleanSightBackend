"""`{track}_playlist.m3u8` 的文本格式 —— 写侧维护的 LIVE 形态清单。

    #EXTM3U
    #EXT-X-VERSION:7
    #EXT-X-TARGETDURATION:10
    #EXT-X-MAP:URI="raw_init.mp4"          ← 整条清单共用一个 init
    #EXTINF:10.013,
    raw_segment_1700000000000000.mp4
    ...                                     ← 每段两行，只追加，不写 ENDLIST

**不写 `#EXT-X-ENDLIST`**：这是写侧的 LIVE 清单，随录随追；VOD 形态（含 ENDLIST、可能
只截其中一段区间）由读侧按需另生成，不动这份。

**EXTINF 是段时长的唯一真值**，不能用相邻段文件名的 ts 差重新推导——那是墙钟量，比媒体
时长少一个帧间隔，断流时还会把整个停顿算进去。清单里的累计 EXTINF 同时是下一段 tfdt 的
落点（`tfdt(N) = Σ EXTINF(0..N-1)`），所以本模块的求和函数是写入事务 ② adjust 步的输入。

**键集合即"已完成转码并登记"的段**：不在清单里的段文件是在途段（或转码失败的残留），
读侧据此过滤。这也是为什么 EXTINF 行必须**最后**追加（W8）。

依赖上界：stdlib only。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# 播放器用它预留缓冲；段长由上游按帧数切，实测 ~10 s。
_TARGET_DURATION = 10

# `#EXTINF:10.013,` —— 逗号后的可选标题本域从不写。
_EXTINF_RE = re.compile(r"^#EXTINF:([0-9.]+),?$")


def header(init_name: str) -> str:
    """清单头（含 `EXT-X-MAP`）。首段登记前写一次，整条清单只有这一份。

    `EXT-X-MAP` 的 URI 是**文件名**不是绝对路径：播放器按相对 playlist 的位置解析，
    而 playlist 与 init 同在 `hls/` 目录里。
    """
    return (
        "#EXTM3U\n"
        "#EXT-X-VERSION:7\n"
        f"#EXT-X-TARGETDURATION:{_TARGET_DURATION}\n"
        f'#EXT-X-MAP:URI="{init_name}"\n'
    )


def entry(duration_s: float, segment_name: str) -> str:
    """一个段的两行条目。三位小数是既有落盘格式，读侧按它解析。"""
    return f"#EXTINF:{duration_s:.3f},\n{segment_name}\n"


def total_duration(playlist: Path) -> float:
    """清单里所有 `#EXTINF` 之和（秒）；文件不存在返回 `0.0`。

    这是「本段之前的媒体轴长度」——在把本段条目追加进去**之前**调用，求和即得本段的
    tfdt 起点。首段读不到任何条目，返回 0.0。

    **读不动就当 0.0**（warning，不抛）：这一步的产物是个偏移量，读失败时 0.0 会让本段
    落回媒体轴原点、与前段重叠——不好，但比整段作废好；而调用方此刻还什么都没登记。
    坏行（EXTINF 里不是数）逐行跳过，判据同 R6：内容坏了隔离，环境坏了才是另一回事。
    """
    if not playlist.exists():
        return 0.0
    total = 0.0
    try:
        with playlist.open("r", encoding="utf-8") as f:
            for raw in f:
                m = _EXTINF_RE.match(raw.strip())
                if m:
                    try:
                        total += float(m.group(1))
                    except ValueError:
                        continue
    except OSError as e:
        logger.warning("[storage.hls] 读清单求累计时长失败，按 0 处理 %s: %s", playlist, e)
        return 0.0
    return max(0.0, total)


def append(playlist: Path, init_name: str, duration_s: float, segment_name: str) -> None:
    """登记一个段：清单不存在则先写头，再追加条目。

    Raises:
        OSError: 写失败。此时段文件已就位但没进清单 —— 表现为一个永久"在途"的段
            （读侧按键集合过滤，不会把它喂给播放器），不是坏数据。

    头与条目分两次 open：头只在首段写一次，把它和条目合并成一次写会让每段都要先判存在
    再决定写什么，反而多一次 stat。
    """
    if not playlist.exists():
        with playlist.open("w", encoding="utf-8") as f:
            f.write(header(init_name))
    with playlist.open("a", encoding="utf-8") as f:
        f.write(entry(duration_s, segment_name))
