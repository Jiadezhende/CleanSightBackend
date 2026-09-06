"""
m3u8 播放列表的读与写。

写侧（persistence/hls_strategy）落的是 LIVE 形态 playlist（不写 ENDLIST，逐段 append
`#EXTINF:<dur>,` + 一行文件名）；VOD 形态由读侧按需动态生成。本模块是这套格式的读侧真源。

**EXTINF 是段时长的唯一真值**，不能用文件名 ts 差重新推导 —— 那是 wall-clock 抖动值，
与 fMP4 fragment 的实际媒体时长不一致，会引入 hls.js 段尾停摆 / 总时长缩水。

依赖上界：stdlib only（L0）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Dict, List, Optional, Sequence, Tuple

from app.services.step_store import layout

if TYPE_CHECKING:  # 仅类型标注，避免 playlist ↔ finder 的模块级互引
    from app.services.step_store.finder import SegmentFinder, SegmentRef

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


def has_init(step_dir: Path, track: str) -> bool:
    """该轨的 fMP4 init 段是否已就位。

    正常落盘的 step 必有 init（首段 transcode 时产出）。缺 init 只剩两种可能：
    ① 段是 `{track}_init.mp4` 命名之前的旧格式产物 —— 不支持，也不提供迁移；
    ② 首段正在 transcode 途中（窗口极短）。

    **两者服务端都无法自愈**，故调用方应按「此 step 不可播放/导出」处理，而不是当成
    「暂时没有」。具体映射成什么错误（HTTP 503 / 领域异常）由调用方决定 —— 判据是格式
    知识，状态码是协议层决定。
    """
    return (step_dir / layout.init_name(track)).exists()


def filter_playable(
    segs: Sequence["SegmentRef"], durations: Dict[str, float]
) -> List["SegmentRef"]:
    """滤掉在途段：只留已完成 transcode+append、在 playlist 里有 EXTINF 的段。

    在途段 = mp4v 已落盘（故 SegmentFinder 扫得到）但 transcode+append 未完成（故不在
    playlist 里）。放它们过去会让 fragment 实际媒体时长与 playlist 声明对不上 ——
    回放侧表现为 hls.js MSE 缓冲洞，导出侧表现为时长错乱。
    """
    return [s for s in segs if s.filename in durations]


def step_time_bounds_us(
    finder: "SegmentFinder", task_id: int, step_id: int
) -> Optional[Tuple[int, int]]:
    """该 step 双轨并集的 (最早段起点, 最晚段终点)，微秒；无可用段时 None。

    **终点必须取 max(seg.ts + EXTINF)，而不是 max(seg.ts)** —— 后者会漏掉最后一段自身
    长度。EXTINF 是 hls.js / fragment 媒体时长的同源真值，对齐到它才能保证前端显示的
    时长与 `<video>.duration` 一致。

    在途段在 playlist 里查不到 EXTINF，跳过（与 `filter_playable` 同一判据）。raw /
    processed 双轨都纳入，取并集的最早起点与最晚终点 —— 两轨段边界不一定对齐，实测有过
    20+ 秒的差，故它表达的是「该 step 有画面的时间跨度」，不等于任一单轨的播放范围。
    """
    step_dir = finder.task_dir(task_id, step_id)
    start_us: Optional[int] = None
    end_us: Optional[int] = None

    for track in layout.VALID_TRACKS:
        durations = parse_playlist_durations(step_dir / layout.playlist_name(track))
        if not durations:
            continue
        for s in finder.list_segments(task_id, step_id, track):
            dur = durations.get(s.filename)
            if dur is None:
                continue
            seg_end_us = s.ts_us + int(round(dur * 1_000_000))
            if start_us is None or s.ts_us < start_us:
                start_us = s.ts_us
            if end_us is None or seg_end_us > end_us:
                end_us = seg_end_us

    if start_us is None or end_us is None:
        return None
    return start_us, end_us


def build_vod_playlist(
    entries: List[Tuple[str, float]],
    map_uri: str,
    target_duration: int,
    media_sequence: Optional[int] = 0,
) -> str:
    """构造 VOD m3u8 文本（含 ENDLIST）。三个消费方共用这一副骨架。

    **只共用骨架，不统一语义**：`entries` 的 URI 形态（token 化 URL / 同目录 basename）、
    时长来源（playlist EXTINF / 相邻段 ts 跨度）、`target_duration` 的取整方式都由调用方
    自己备料。这些是真差异而非重复 —— 例如 ClipBuilder 的 seek 基准是 ts，改用 playlist
    EXTINF 会让逐段 seek 错位。

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
