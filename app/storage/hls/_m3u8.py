"""`{track}_playlist.m3u8` 的文本格式 —— 写侧维护的 LIVE 形态清单。

    #EXTM3U
    #EXT-X-VERSION:7
    #EXT-X-TARGETDURATION:10
    #EXT-X-MAP:URI="raw_init.mp4"          ← 整条清单共用一个 init
    #EXTINF:10.013,
    raw_segment_1700000000000000.mp4
    ...                                     ← 每段两行，只追加，不写 ENDLIST

## 本模块只管这一份 LIVE 清单，写一路 + 读一路

    写侧   header / entry / append / total_duration   建清单、追条目、求累计（tfdt 输入）
    读侧   durations                                  逐段 EXTINF，服务 `playable_segments`

三条格式事实（全部会静默出错，别绕开）：

- **不写 `#EXT-X-ENDLIST`**：这是随录随追的 LIVE 清单；VOD 形态由读侧按需另生成，不动这份。
- **EXTINF 是段时长的唯一真值**，不能用相邻段文件名的 ts 差重推（那是墙钟量，比媒体时长多
  出帧间隔与断流停顿）。累计 EXTINF 同时是下一段 tfdt 的落点，故求和函数是写入事务
  ② adjust 步的输入。
- **键集合即"已完成转码并登记"的段**，读侧据此过滤在途段；所以 EXTINF 行必须**最后**追加。

**VOD 形态不在本域**（→ `app/services/utils/vod_playlist.py`）：本模块只出事实——哪些段登记
了、各自多长；怎么拼成一份清单是服务层的判断。

依赖上界：stdlib only。
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import Dict

logger = logging.getLogger(__name__)

# 播放器用它预留缓冲；段长由上游按帧数切，实测 ~10 s。
_TARGET_DURATION = 10

# `#EXTINF:10.013,` —— 逗号后的可选标题本域从不写。
_EXTINF_RE = re.compile(r"^#EXTINF:([0-9.]+),?$")


def header(init_name: str) -> str:
    """清单头（含 `EXT-X-MAP`）。首段登记前写一次，整条清单只有这一份。

    `EXT-X-MAP` 的 URI 是**文件名**不是绝对路径（播放器按相对 playlist 的位置解析）。
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

    这是「本段之前的媒体轴长度」= 本段的 tfdt 起点，**必须在追加本段条目之前调用**。
    读不动时按 0.0 处理（warning，不抛），坏行逐行跳过。
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
        OSError: 写失败。此时段文件已就位但没进清单，表现为一个永久"在途"的段（读侧按键
            集合过滤，不会把它喂给播放器），不是坏数据。
    """
    if not playlist.exists():
        with playlist.open("w", encoding="utf-8") as f:
            f.write(header(init_name))
    with playlist.open("a", encoding="utf-8") as f:
        f.write(entry(duration_s, segment_name))


# ---------------------------------------------------------------------------
# 读侧：逐段 EXTINF
# ---------------------------------------------------------------------------


def durations(playlist: Path) -> Dict[str, float]:
    """段文件名 → EXTINF 秒。文件不存在或读不动返回 `{}`（warning，不抛）。

    返回值同时承担两个职责，故不拆成两个函数：**值**是段时长的唯一真值，**键集合**即
    "已完成转码并登记"的段。带标题的手写条目按坏行跳过。

    **保持包内私有**：消费方都被 `_read.playable_segments` 覆盖（它把键集合与值一次给全）。
    """
    if not playlist.exists():
        return {}
    try:
        with playlist.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("[storage.hls] 读清单逐段时长失败，按空处理 %s: %s", playlist, e)
        return {}

    out: Dict[str, float] = {}
    pending: float | None = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            m = _EXTINF_RE.match(line)
            if m is None:
                pending = None
                continue
            try:
                pending = float(m.group(1))
            except ValueError:
                pending = None
        elif line and not line.startswith("#"):
            # 非注释行 = URI 行。只有紧跟在合法 EXTINF 之后才算一个完整条目。
            if pending is not None:
                out[line] = pending
            pending = None
    return out
