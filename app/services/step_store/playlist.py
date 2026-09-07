"""
m3u8 播放列表的读与写。

写侧（persistence/hls_strategy）落的是 LIVE 形态 playlist（不写 ENDLIST，逐段 append
`#EXTINF:<dur>,` + 一行文件名）；VOD 形态由读侧按需动态生成。本模块是这套格式的读侧真源。

**EXTINF 是段时长的唯一真值**，不能用文件名 ts 差重新推导 —— 那是 wall-clock 抖动值，
与 fMP4 fragment 的实际媒体时长不一致，会引入 hls.js 段尾停摆 / 总时长缩水。

## 本模块**包内私有**，函数都收「已定位的具体路径」

服务层不该调这里——它们不知道 playlist 文件叫什么、在哪个目录，也不该知道。对外入口
是 `Step` 的成员（`segments()` / `has_init()` / `time_bounds_us` / `vod_playlist()`），
存储根只由本包自解析一次。**对外只出 m3u8 成品、不出骨架**：只出骨架等于要求每个
调用方自己备料，而备料（EXTINF 真值、滤在途、判 init、算 TARGETDURATION）才是写错会
静默的那部分——骨架写错播放器立刻报错。

包外只有两条**具名例外**（由 tests/test_import_hygiene.py 的门禁锁死）：

    persistence/strategies/hls_strategy  写侧，它本就持有目录、是格式的**定义者**
                                         （`sum_durations` 算 tfdt 前缀和、手写 EXTINF 行）
    lab/clip_builder                     每段 EXTINF 取相邻段 ts 跨度而非 playlist
                                         EXTINF（seek 基准是 ts，换了会逐段错位）。
                                         验证两者等价后收进 `Step.vod_playlist`，届时
                                         这条例外撤销

依赖上界：stdlib only（L0）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

_EXTINF_PREFIX = "#EXTINF:"


def parse_playlist_entries(playlist_path: Path) -> List[Tuple[str, float]]:
    """解析写入侧 LIVE m3u8，按出现顺序产出 [(filename, 时长秒)]。

    格式约定：每个段对应一行 `#EXTINF:<dur>,` 紧接一行 `<filename>`（见 hls_strategy
    的 playlist append）。解析失败或文件不存在返回空列表。

    **保序**是有意的：写侧 tfdt 偏移要的是「当前段以前所有 EXTINF 的前缀和」，顺序即语义；
    读侧要的是 filename → dur 映射（见 `parse_playlist_durations`）。一份解析供两种消费。
    """
    if not playlist_path.exists():
        return []
    try:
        with playlist_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("[Playlist] 读取失败 %s: %s", playlist_path, e)
        return []

    entries: List[Tuple[str, float]] = []
    pending_dur: Optional[float] = None
    for raw in lines:
        line = raw.strip()
        if line.startswith(_EXTINF_PREFIX):
            try:
                # "#EXTINF:1.234," → 1.234
                pending_dur = float(line[len(_EXTINF_PREFIX) :].rstrip(",").strip())
            except ValueError:
                pending_dur = None
        elif line and not line.startswith("#"):
            if pending_dur is not None:
                entries.append((line, pending_dur))
            pending_dur = None
    return entries


def parse_playlist_durations(playlist_path: Path) -> Dict[str, float]:
    """filename → EXTINF 时长映射。

    返回值同时承担两个职责：
    - EXTINF 是段时长唯一真值，不能用文件名 ts 差重新推导；
    - 键集合即「已完成 transcode+append 的段」，不在其中的是在途段，须过滤。

    解析失败或文件不存在返回空字典。
    """
    return dict(parse_playlist_entries(playlist_path))


def sum_durations(playlist_path: Path) -> float:
    """playlist 内全部 EXTINF 之和（秒），非负。

    写侧算 fMP4 fragment 的 tfdt 起点用：`tfdt(N) = Σ EXTINF(0..N-1)`。调用发生在
    把当前段 append 进 playlist **之前**，故此刻求和即得本段起点。

    ⚠ 不要回退到「文件名 ts_us 差」的算法 —— 那是 wall-clock 抖动值，与 fragment
    媒体时长不一致，会重新引入 hls.js 段尾停摆 / 总时长缩水 bug。
    """
    return max(0.0, sum(dur for _, dur in parse_playlist_entries(playlist_path)))


def build_vod_playlist(
    entries: List[Tuple[str, float]],
    map_uri: str,
    target_duration: int,
    media_sequence: Optional[int] = 0,
) -> str:
    """构造 VOD m3u8 文本（含 ENDLIST）。**骨架，非成品** —— 对外入口是
    `Step.vod_playlist()`，它在这之上补齐全部备料。

    **只共用骨架，不统一语义**：`entries` 的 URI 形态（token 化 URL / 同目录 basename）、
    时长来源（playlist EXTINF / 相邻段 ts 跨度）、`target_duration` 的取整方式都由调用方
    自己备料。这些是真差异而非重复 —— 例如 ClipBuilder 的 seek 基准是 ts，改用 playlist
    EXTINF 会让逐段 seek 错位。

    纯字符串拼装、不碰磁盘。

    Args:
        entries: [(段 URI, 时长秒)]，按播放顺序。调用方保证非空、已排序。
        map_uri: EXT-X-MAP 的 URI（fMP4 fragment 无 init 段无法解码）。
        target_duration: EXT-X-TARGETDURATION 的整数秒值。
        media_sequence: EXT-X-MEDIA-SEQUENCE 的值；传 None 则不写该行。
    """
    lines = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{target_duration}",
    ]
    if media_sequence is not None:
        lines.append(f"#EXT-X-MEDIA-SEQUENCE:{media_sequence}")
    lines.append(f'#EXT-X-MAP:URI="{map_uri}"')
    for uri, dur in entries:
        lines.append(f"#EXTINF:{dur:.3f},")
        lines.append(uri)
    # 缺 ENDLIST → 播放端当直播流只读 live edge，前面的段全丢
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"
