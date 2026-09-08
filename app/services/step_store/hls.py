"""HLS 视频落盘域：这个 step 的视频是怎么落盘的、怎么读回来。

    from app.services.step_store import hls
    hls.segments(task_id, step_id, "raw")
    hls.vod_playlist(task_id, step_id, "processed", encode_uri=...)

**不出句柄、不出目录、不出文件名**：全部是收 `(task_id, step_id, ...)` 的模块函数，调用方只
知道「传 id 和参数，拿到想要的信息」。段/init/playlist/sidecar 叫什么、m3u8 长什么样、在途段
怎么判，全在包内（`_layout` / `_playlist`）。

**与 `store` 的分工**：目录在哪、目录里能不能开文件、整个目录怎么删，是所有域共享的问题，在
`store`；本模块只管视频这一摊，落盘位置一律经 `store` 的文件出入口，活动时间戳因此自动刷新。

**无跨调用缓存**：每个函数自带它需要的那次目录扫描。故函数是按「一次调用 = 一件事」切的
（`list_steps` 一次出双轨段清单、`vod_playlist` 一次出成品），不是照搬取值器——照搬会让一个
请求扫好几遍盘。
"""

from __future__ import annotations

import logging
import os
import threading
from array import array
from contextlib import contextmanager
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Callable,
    Dict,
    Iterator,
    List,
    NamedTuple,
    Optional,
    Sequence,
    Tuple,
)

from app.services.step_store import _layout, _playlist, store
from app.services.step_store._layout import SegmentRef

if TYPE_CHECKING:  # 仅类型标注：Frame 属 domain（L0），但 _decoder 吃 numpy
    from app.domain.frame import Frame

logger = logging.getLogger(__name__)

__all__ = [
    "SegmentRef",
    "StepSegments",
    "HlsError",
    "HlsTrackMissing",
    "HlsInitMissing",
    "HlsNoPlayableSegments",
    "list_steps",
    "segments",
    "time_bounds_us",
    "has_init",
    "vod_playlist",
    "frames",
    "playlist_entries",
    "write_segment",
    "SegmentWrite",
    "HlsConcurrentWrite",
]


# ---------------------------------------------------------------------------
# 领域异常：表达「这个 step 的这条轨播不了」，映射成什么状态码由调用方决定
# ---------------------------------------------------------------------------


class HlsError(Exception):
    """HLS 域异常基类。"""


class HlsTrackMissing(HlsError):
    """该轨在磁盘上一个段都没有（这个 step 没录过这条轨）。"""


class HlsInitMissing(HlsError):
    """该轨缺 fMP4 init 段，fragment 无法解码。

    只有两种可能：旧格式产物（无迁移路径），或首段仍在 transcode（窗口极短）。**服务端都
    无法自愈**，故调用方按「此 step 不可播放/导出」处理。
    """


class HlsNoPlayableSegments(HlsError):
    """该轨有段，但没有一个已完成落盘（全部在途）。"""


class StepSegments(NamedTuple):
    """一个 step 的双轨段清单，一次目录扫描的产物。

    `by_track` **只含实际有段的轨**（空轨不出现），故 `by_track.keys()` 就是「这个 step 录了
    哪几条轨」，且**按 `_layout.VALID_TRACKS` 的顺序**（raw, processed）—— 对外接口的 tracks
    字段直接 `list(by_track)` 即可，顺序稳定。清单类接口（大屏历史、lab 任务列表）要的正是
    这一份，不必再逐轨问一遍。
    """

    task_id: int
    step_id: int
    by_track: Dict[str, List[SegmentRef]]


# ---------------------------------------------------------------------------
# 包内私有：扫描与解析
# ---------------------------------------------------------------------------


def _check_track(track: str) -> str:
    if track not in _layout.VALID_TRACKS:
        raise ValueError(
            f"Invalid track: {track!r}, expected one of {_layout.VALID_TRACKS}"
        )
    return track


def _scan(task_id: int, step_id: int) -> Dict[str, List[SegmentRef]]:
    """单次 iterdir 扫出按轨分组的段（各轨内按 ts_us 升序），双轨只付一次目录遍历。
    目录不存在时返回各轨空列表。"""
    by_track: Dict[str, List[SegmentRef]] = {t: [] for t in _layout.VALID_TRACKS}
    step_path = store._step_dir(task_id, step_id)
    if not step_path.is_dir():
        return by_track
    for entry in step_path.iterdir():
        if not entry.is_file():
            continue
        parsed = _layout.parse_segment_name(entry.name)
        if parsed is None:
            continue
        track, ts_us = parsed
        by_track[track].append(SegmentRef(filename=entry.name, ts_us=ts_us))
    for refs in by_track.values():
        refs.sort(key=lambda r: r.ts_us)
    return by_track


def _extinf(task_id: int, step_id: int, track: str) -> Dict[str, float]:
    """该轨 filename → EXTINF 时长映射。

    EXTINF 是段时长**唯一真值**（不能用文件名 ts 差重推）；键集合同时是「已完成
    transcode+append」的判据 —— 不在其中的是在途段。
    """
    return _playlist.parse_playlist_durations(
        store._step_dir(task_id, step_id) / _layout.playlist_name(track)
    )


def _playable(
    segs: Sequence[SegmentRef], durations: Dict[str, float]
) -> List[SegmentRef]:
    return [s for s in segs if s.filename in durations]


# ---------------------------------------------------------------------------
# 读
# ---------------------------------------------------------------------------


def list_steps(
    task_id: Optional[int] = None, playable_only: bool = False
) -> List[StepSegments]:
    """列出**有视频段**的 step 及其双轨段清单，按 (task_id, step_id) 升序。

    Args:
        task_id: None 表示全局枚举；给了则只列该 task 的。
        playable_only: 默认 False =「磁盘上有什么就报什么」，供只报有没有画面、不解码也不拼
            m3u8 的清单类接口用。True 则滤掉在途段（语义见 `segments`）。

    **两轨都没段的 step 不出现在结果里** —— 起流即失败留下的空目录对回放没意义，清单不该把它
    露给前端点开黑屏。TTL 恰恰要看见这类目录，那边用 `store.steps()`。
    """
    out: List[StepSegments] = []
    for tid, sid in store.steps(task_id):
        by_track = _scan(tid, sid)
        if playable_only:
            by_track = {
                t: _playable(segs, _extinf(tid, sid, t))
                for t, segs in by_track.items()
            }
        non_empty = {t: segs for t, segs in by_track.items() if segs}
        if non_empty:
            out.append(StepSegments(task_id=tid, step_id=sid, by_track=non_empty))
    return out


def segments(
    task_id: int, step_id: int, track: str, playable_only: bool = True
) -> List[SegmentRef]:
    """该轨全部段，ts 升序。

    Args:
        playable_only: **默认 True，滤掉在途段**（mp4 已落盘故扫得到，但 transcode+append
            未完成故不在 playlist 里）。放它们过去，fragment 实际媒体时长会与 playlist 声明
            对不上：回放侧是 hls.js MSE 缓冲洞，导出侧是时长错乱。传 False =「磁盘上有什么就
            报什么」，供只报有没有画面、不解码也不拼 m3u8 的调用方用。

    Raises:
        ValueError: track 非法
    """
    _check_track(track)
    segs = _scan(task_id, step_id)[track]
    if not playable_only:
        return segs
    return _playable(segs, _extinf(task_id, step_id, track))


def time_bounds_us(task_id: int, step_id: int) -> Optional[Tuple[int, int]]:
    """双轨并集的 (最早段起点, 最晚段终点)，微秒；无可用段时 None。在途段查不到 EXTINF，跳过。

    **终点取 max(seg.ts + EXTINF) 而非 max(seg.ts)** —— 后者漏掉最后一段自身长度。取并集是
    因为两轨段边界不一定对齐（实测有过 20+ 秒差），故它表达「该 step 有画面的时间跨度」，不
    等于任一单轨的播放范围。
    """
    by_track = _scan(task_id, step_id)
    start_us: Optional[int] = None
    end_us: Optional[int] = None
    for track in _layout.VALID_TRACKS:
        by_name = _extinf(task_id, step_id, track)
        if not by_name:
            continue
        for s in by_track[track]:
            dur = by_name.get(s.filename)
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


def has_init(task_id: int, step_id: int, track: str) -> bool:
    """该轨的 fMP4 init 段是否已就位。缺了意味着什么见 `HlsInitMissing`。"""
    _check_track(track)
    return (
        store._step_dir(task_id, step_id) / _layout.init_name(track)
    ).exists()


def vod_playlist(
    task_id: int,
    step_id: int,
    track: str,
    segments: Optional[Sequence[SegmentRef]] = None,
    encode_uri: Optional[Callable[[str, str], str]] = None,
) -> str:
    """该轨的 VOD m3u8 **成品**文本（含 `#EXT-X-ENDLIST`）。

    Args:
        segments: 要收进 playlist 的段；None 表示整轨可播段。显式传入时会**再滤一道在途段**
            —— 调用方若来自不滤在途段的查询，playlist 声明的时长会与 fragment 实际媒体时长
            对不上，表现为 hls.js 缓冲洞。
        encode_uri: `(kind, filename) -> uri`，`kind ∈ {"segment", "init"}`。默认恒等（裸文件
            名在与 init/段同目录的 m3u8 里是合法相对 URI）。要 token 化 URL 的从这里注入。

    Raises:
        HlsTrackMissing: 该轨磁盘上一个段都没有（仅在 `segments` 为 None 时判定 —— 显式传段
            的调用方自己已经决定了收哪些）
        HlsInitMissing: 该轨缺 init 段
        HlsNoPlayableSegments: 有段但全在途

    三个异常的**判定顺序即调用方的状态码顺序**，别调换：没录过这条轨（404）→ 录了但不可解码
    （503）→ 录了但还没就绪（404，措辞不同）。

    `#EXT-X-TARGETDURATION` 取 `round(max EXTINF)` 而非 ceil：RFC 8216 §4.3.2.1 的判据就是
    「EXTINF 四舍五入后 MUST ≤ TARGETDURATION」。
    """
    _check_track(track)

    if segments is None:
        candidates: List[SegmentRef] = _scan(task_id, step_id)[track]
        if not candidates:
            raise HlsTrackMissing(
                f"No {track} segments for task {task_id} step {step_id}"
            )
    else:
        candidates = list(segments)

    if not has_init(task_id, step_id, track):
        raise HlsInitMissing(
            f"{_layout.init_name(track)} not found for task {task_id} "
            f"step {step_id}. This step is either mid-transcode or written "
            f"in an unsupported legacy layout."
        )

    durations = _extinf(task_id, step_id, track)
    playable = _playable(candidates, durations)
    if not playable:
        raise HlsNoPlayableSegments(
            f"No playable {track} segments for task {task_id} "
            f"step {step_id} (no segments, or all in-flight)"
        )

    uri = encode_uri if encode_uri is not None else (lambda _kind, name: name)
    seg_durs = [durations[s.filename] for s in playable]
    return _playlist.build_vod_playlist(
        entries=[(uri("segment", s.filename), d) for s, d in zip(playable, seg_durs)],
        map_uri=uri("init", _layout.init_name(track)),
        target_duration=max(int(round(max(seg_durs))), 1),
    )


def frames(
    task_id: int,
    step_id: int,
    track: str = "raw",
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
    width: int = 640,
    height: int = 480,
) -> Iterator["Frame"]:
    """把 `[start_ts, end_ts]` 区间解码成像素帧（惰性流，ts 升序）。None 表示该侧不设限。
    ⚠ **会起 ffmpeg 子进程**，每段一个。

    缺 sidecar 的段**跳过、不抛** —— 宽容留在这层（缺一段的索引不该让前后所有段一起读不了），
    严格留在消费侧（`inference/offline/frame_finder.FrameFinder` 按 ts 对号，配不上就
    ValueError）。
    """
    _check_track(track)
    # 函数体内 import：_decoder 吃 numpy，顶层 import 会让「只想拿个段清单」的调用方也付这
    # 笔钱；同时避开 _decoder 回指本模块取段列表造成的模块级循环。
    from app.services.step_store._decoder import SegmentDecoder

    yield from SegmentDecoder.for_step(task_id, step_id, track).iter(
        start_ts, end_ts, width, height
    )




# ---------------------------------------------------------------------------
# 写 —— 一段视频落盘是一个原子单元，故只有一个事务入口
# ---------------------------------------------------------------------------


def playlist_entries(
    task_id: int, step_id: int, track: str
) -> List[Tuple[str, float]]:
    """该轨 LIVE playlist 里的 [(段文件名, EXTINF 秒)]，按写入顺序。文件不存在返回空。

    给排查/度量工具用（对齐检查、tfdt 前缀和核对）。回放拼 m3u8 走 `vod_playlist`。
    """
    _check_track(track)
    return _playlist.parse_playlist_entries(
        store._step_dir(task_id, step_id) / _layout.playlist_name(track)
    )


class HlsConcurrentWrite(HlsError):
    """同一 (step, track) 上有两个并发的 `write_segment`。

    HLS 写侧是**单线程**（`HLSWorkerPool` 固定一个 worker）。两个并发写者会读到相同的累计
    EXTINF，两段 fragment 的 tfdt 起点撞在一起 —— hls.js 播到第二段停在段尾不前进。这条守卫
    不串行化任何东西，只是让「单写者」这个前提在被打破时**响亮地失败**，而不是静默出错。
    """


# 在途写入的 (task_id, step_id, track)。`_inflight_guard` 只保护这个集合的增删，
# **不保护写入过程** —— 它不是把并发变成串行，是把并发变成异常。
_inflight: set = set()
_inflight_guard = threading.Lock()


class SegmentWrite:
    """`write_segment` 事务的把手：调用方往 `stage_path` 写字节，`commit()` 交回时长与帧 ts。

    调用方只拿到「往哪写」和「tfdt 该从哪开始」，**不知道最终文件叫什么、playlist 长什么样、
    sidecar 怎么命名** —— 那些连同提交顺序都在 `write_segment` 的退出路径里。
    """

    __slots__ = (
        "stage_path",
        "init_stage_path",
        "tfdt_offset_s",
        "_duration_s",
        "_frame_timestamps",
        "_committed",
    )

    def __init__(
        self,
        stage_path: Path,
        init_stage_path: Optional[Path],
        tfdt_offset_s: float,
    ):
        self.stage_path = stage_path
        self.init_stage_path = init_stage_path
        self.tfdt_offset_s = tfdt_offset_s
        self._duration_s: float = 0.0
        self._frame_timestamps: Optional[Sequence[float]] = None
        self._committed = False

    def commit(
        self, duration_s: float, frame_timestamps: Optional[Sequence[float]] = None
    ) -> None:
        """声明本段已产出完毕，交出提交所需的两样东西。真正的落盘在事务退出时发生。

        Args:
            duration_s: 本段 EXTINF。**必须与 fragment 实际媒体时长完全一致**（写侧是
                `len(frames)/eff_fps`，与编码时用的 fps 同源）—— 对不上就是 hls.js 段尾 MSE
                缓冲洞 + 总时长缩水。它同时是下一段 tfdt 起点的加数。
            frame_timestamps: 该段每帧的 ts，落成 sidecar 供离线按 ts 反查帧。**只有 raw 轨产
                sidecar**（processed 是渲染结果，离线不消费），processed 传 None。
        """
        self._duration_s = float(duration_s)
        self._frame_timestamps = frame_timestamps
        self._committed = True


@contextmanager
def write_segment(
    task_id: int, step_id: int, track: str, start_ts: float
) -> Iterator[SegmentWrite]:
    """落一段视频：**一个原子单元**（sidecar + 段 mp4 + init + playlist 的 EXTINF 行）。

    调用方在 with 体内把字节产出到 `seg.stage_path`（该轨首段还要把 init 产出到
    `seg.init_stage_path`），然后 `seg.commit(duration_s, frame_timestamps)`。正常退出时本函数
    按序提交；**未 commit 或抛异常则一律回滚** —— 临时文件清掉，playlist 一个字节不动。

    Args:
        start_ts: 本段首帧 ts（**秒**）。段名里的 `ts_us` 由本函数换算，走的是**截断**而非四舍
            五入 —— 读侧按 ts 定位段依赖 `ts_us <= ts*1e6`，改成 round 会让「start_ts 恰为该段
            首帧」的定位无条件出错。

    Raises:
        ValueError: track 非法
        HlsConcurrentWrite: 同一 (step, track) 已有在途写入（见该异常）

    提交顺序不能改，四条都是写错会**静默**出错的：

    1. **sidecar 先于段 mp4 可见** —— 读侧的段扫描认的是 `{track}_segment_*.mp4`，mp4 一出现该
       段就对离线反查可见。反过来会留下「段可见但索引未就位」的窗口，期间读侧拿不到 sidecar。
    2. **sidecar 与段同 stem** —— 对不上时读侧按契约只 warning 跳过该段，表现为静默丢帧。
    3. **段 mp4 就位后才 append EXTINF** —— playlist 里有一行却没有对应文件，播放器直接报错。
    4. **`tfdt_offset_s` 在 append 之前读** —— 它是「当前段以前所有 EXTINF 之和」，append 完再
       读就多算了本段自己。

    **sidecar 写失败不阻断本段**：它只服务离线反查，回放/下载/送标三条链路都不读它；让主产物给
    辅助索引陪葬不划算。读侧本就按契约容忍缺 sidecar（`_decoder._load_sidecar` 跳过该段）。
    """
    _check_track(track)
    ts_us = _layout.ts_to_us(start_ts)
    key = (task_id, step_id, track)

    with _inflight_guard:
        if key in _inflight:
            raise HlsConcurrentWrite(
                f"另一个 write_segment 正在写 task={task_id} step={step_id} "
                f"track={track}；HLS 写侧应为单线程"
            )
        _inflight.add(key)

    step_dir = store._step_dir(task_id, step_id)
    playlist_path = store.file_path(task_id, step_id, _layout.playlist_name(track))
    stage = store.scratch_path(task_id, step_id, f"stage_{track}_{ts_us}", ".mp4")
    # 该轨已有 init 就不再收：两轨各有各的 init，同轨同编码参数下 SPS/PPS 一致，重复产出丢弃。
    init_stage = (
        None
        if has_init(task_id, step_id, track)
        else store.scratch_path(task_id, step_id, f"init_{track}", ".mp4")
    )
    seg = SegmentWrite(
        stage_path=stage,
        init_stage_path=init_stage,
        # append 之前读，故求和即得本段的 tfdt 起点；首段读不到条目，返回 0.0
        tfdt_offset_s=_playlist.sum_durations(playlist_path),
    )

    try:
        yield seg
        if seg._committed:
            _commit_segment(seg, step_dir, playlist_path, track, ts_us)
    finally:
        with _inflight_guard:
            _inflight.discard(key)
        # 提交成功后这两个已被 rename 走；失败/未 commit 则在此清掉，不留残骸
        stage.unlink(missing_ok=True)
        if init_stage is not None:
            init_stage.unlink(missing_ok=True)


def _commit_segment(
    seg: SegmentWrite,
    step_dir: Path,
    playlist_path: Path,
    track: str,
    ts_us: int,
) -> None:
    """按序提交一段。顺序的四条理由见 `write_segment` docstring。"""
    segment_name = _layout.segment_name(track, ts_us)

    # 1. sidecar（只有 raw 轨产），先于段 mp4 可见。失败只 warning，不拖累主产物。
    if seg._frame_timestamps is not None:
        _write_sidecar(
            step_dir / _layout.sidecar_name(track, ts_us), seg._frame_timestamps
        )

    # 2. 段 mp4 就位（rename 是原子的，读侧不会看到半截文件）
    os.replace(seg.stage_path, step_dir / segment_name)

    # 3. init：该轨首段才装
    if seg.init_stage_path is not None and seg.init_stage_path.exists():
        init_path = step_dir / _layout.init_name(track)
        if not init_path.exists():
            os.replace(seg.init_stage_path, init_path)

    # 4. playlist：首次写头（EXT-X-MAP 必须在任何 EXTINF 之前），再 append 本段
    if not playlist_path.exists():
        playlist_path.write_text(
            _playlist.live_header(_layout.init_name(track)), encoding="utf-8"
        )
    with playlist_path.open("a", encoding="utf-8") as f:
        f.write(_playlist.entry_line(segment_name, seg._duration_s))


def _write_sidecar(idx_path: Path, timestamps: Sequence[float]) -> None:
    """把每帧 ts 写成 float64 原值数组（无 tick、无 first_ts）。tmp + os.replace 原子替换。

    用 stdlib `array("d")` 而非 numpy：与读侧的 `np.fromfile(dtype=np.float64)` 逐字节等价
    （都是本机序 IEEE754 double），但省掉一个「只想拿段清单」的调用方也要付的 numpy import。

    **失败只 warning**：理由见 `write_segment`。临时文件用 `.tmp` 后缀即可 —— 它落在段名正则
    之外，不会被 `segments()` 当成真段。
    """
    tmp = idx_path.with_suffix(".tmp")
    try:
        tmp.unlink(missing_ok=True)
        with tmp.open("wb") as f:
            array("d", timestamps).tofile(f)
        os.replace(tmp, idx_path)
    except OSError as e:
        logger.warning(
            "[HLS] sidecar 写入失败，该段离线不可反查（视频照常落盘）: %s: %s", idx_path, e
        )
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass  # 清残留本身也可能失败（同一个盘的同一个故障），别盖掉上面的告警
