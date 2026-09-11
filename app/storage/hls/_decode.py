"""段 fMP4 → 内存 `Frame` —— `_encode` 的读向对称件。

    ref = hls.insert_segment(task_id, step_id, "raw", frames)   # 交帧，拿身份键
    list(hls.read_segment(task_id, step_id, ref, width=W, height=H))  # 交身份键，拿帧

**帧带的是墙钟 ts，不是媒体轴时刻**——后者没有意义：媒体轴被 EXTINF/tfdt 压紧过，段与段
之间的墙钟断流在那条轴上不存在（见 `_fmp4` 的 TIMESCALE 说明）。墙钟 ts 只存在于 sidecar，
所以本模块的每一帧都是「像素来自 mp4、时间来自 `.idx`」的合成物。

## 段内帧号 = sidecar 下标（这条契约是解码方式的直接后果）

写侧逐帧写 ts、cv2 按 N 帧写出 N 帧，两者天然 1:1。读侧要维持它，解码命令只能长成这样：

    concat:{raw_init.mp4}|{raw_segment_*.mp4}   拼 init 才解得开 fragment
    -vf select=between(n,k1,k2)                 按**帧号**选，不是按时间
    不用 -ss                                    它按时间 seek，会让 n 的原点漂掉

**给这条命令加 `-ss` 优化不会报错**，表现是帧号整体平移、反查回来的是错帧，而
`FrameTracker.find` 的位级 ts 比较会把它当"没找到"抛 ValueError——错因指向完全错误的方向。
这正是这套知识必须待在写侧隔壁的理由。

## 只服务 raw 轨

`processed` **不落 sidecar**（`_idx` / `_write` 的有意不对称）：它是画完框的渲染结果，
离线反查要的是原始帧，拿渲染结果构造视觉特征等于把模型自己的输出再喂回去。故
`iter_frames` 连 `track` 参数都不设，`read_segment` 收到非 raw 的 ref 直接 `ValueError`
——不是静默返回空，「这条路不通」与「这段没数据」必须分得开。

## 区间语义

`start_ts` / `end_ts` 是**闭区间**的墙钟秒，`None` 表示该侧不设限。两级裁剪：段级选出
可能命中的段（省 ffmpeg 调用次数），帧级在段内选 `[k_start, k_end]`（不解无效像素）。

依赖上界：`app.domain`（域货币 `Frame`）+ numpy（sidecar 的货币）+ stdlib。ffmpeg 是
**运行时**依赖（D5），`settings.ffmpeg_path` 按 `_fmp4` 的规矩只在函数体内 import。
"""

from __future__ import annotations

import logging
import subprocess
import tempfile
import threading
from typing import Iterator, List, Optional

import numpy as np

from app.domain.frame import Frame

from . import _idx, _layout
from ._layout import SegmentRef

logger = logging.getLogger(__name__)

# 本模块唯一服务的轨道。见模块 docstring 的「只服务 raw 轨」。
_RAW_TRACK = "raw"

# 单段解码的 ffmpeg 预算，口径同写侧：max(下限, 规模 × 单位)。一段是有界工作量
#（≤ sidecar 条数），故按帧给预算而非给全局超时。实测整段 150 帧解码 73 ms，
# 0.2 s/帧 余量约 400×，只用于兜「坏盘/网络盘上永久阻塞」。
_DECODE_TIMEOUT_FLOOR_S = 30
_DECODE_TIMEOUT_PER_FRAME_S = 0.2

# 失败时带进异常的 ffmpeg stderr 尾部长度
_STDERR_TAIL_CHARS = 500


def _require_raw(track: str) -> None:
    """非 raw 轨一律拒绝，并把理由写进消息——它不是"暂不支持"，是不该问。"""
    if track != _RAW_TRACK:
        raise ValueError(
            f"解码只服务 {_RAW_TRACK!r} 轨，收到 {track!r}："
            "processed 是画完框的渲染结果、按契约不落 sidecar，给不出带墙钟 ts 的帧；"
            "离线反查要的本就是原始帧"
        )


def _read_exact(stream, buf: bytearray) -> bool:
    """把 stream 读满 buf；EOF 提前到达返回 False。

    `readinto` 直接写进调用方给的 buffer，比 `read(n)` 少一次 bytes 分配 + 拷贝；
    管道上单次 readinto 可能短读，故循环填满。
    """
    view = memoryview(buf)
    got = 0
    while got < len(buf):
        n = stream.readinto(view[got:])
        if not n:
            return False
        got += n
    return True


def _build_cmd(
    task_id: int, step_id: int, ref: SegmentRef, start: int, end: int, width: int, height: int
) -> List[str]:
    """解出 `[start, end]` 闭区间帧号的 ffmpeg 命令（rawvideo bgr24 走管道）。

    三处不能动的地方，全部在模块 docstring 的「段内帧号 = sidecar 下标」里有账：
    `concat:` 拼 init、`select` 按帧号、**没有 `-ss`**。此外 `select` 必须排在 `scale`
    之前——反过来会把注定被丢弃的帧也缩放一遍。
    """
    from app.settings import settings  # 规范 §2 通路 2，同 `_fmp4.transcode`

    init = _layout.init_path(task_id, step_id, _RAW_TRACK)
    segment = _layout.segment_path(task_id, step_id, ref)
    return [
        settings.ffmpeg_path,
        "-loglevel", "error", "-hide_banner",
        "-i", f"concat:{init}|{segment}",
        "-vf", f"select=between(n\\,{start}\\,{end}),scale={width}:{height}",
        "-vframes", str(end - start + 1),
        "-vsync", "0",
        "-f", "rawvideo",
        "-pix_fmt", "bgr24",
        "pipe:1",
    ]


def _decode_failure(
    ref: SegmentRef,
    k: int,
    k_start: int,
    k_end: int,
    timed_out: threading.Event,
    budget: float,
    errf,
) -> str:
    """拼解码失败信息，带上 ffmpeg stderr 尾部（否则定位只能靠猜）。"""
    tail = ""
    try:
        errf.seek(0)
        tail = errf.read().decode("utf-8", "replace").strip()[-_STDERR_TAIL_CHARS:]
    except OSError:
        pass
    what = f"ffmpeg timeout after {budget:g}s" if timed_out.is_set() else "Incomplete frame"
    msg = (
        f"{what} from {_layout.segment_name(ref)} @ 段内帧 {k}"
        f"（请求区间 [{k_start},{k_end}]）"
    )
    return f"{msg}; ffmpeg stderr: {tail}" if tail else msg


def _run_ffmpeg(
    task_id: int,
    step_id: int,
    ref: SegmentRef,
    sidecar: np.ndarray,
    k_start: int,
    k_end: int,
    width: int,
    height: int,
) -> Iterator[Frame]:
    """解出段内 `[k_start, k_end]` 闭区间的帧，逐帧 yield。

    像素来自管道、ts 来自 `sidecar[k]` —— 二者靠帧号对齐，见模块 docstring。

    这是本域**唯一的解码 I/O 边界**，测试把它换掉就能不起 ffmpeg 覆盖全部裁剪数学。
    """
    frame_size = width * height * 3
    n_frames = k_end - k_start + 1
    budget = max(_DECODE_TIMEOUT_FLOOR_S, n_frames * _DECODE_TIMEOUT_PER_FRAME_S)
    cmd = _build_cmd(task_id, step_id, ref, k_start, k_end, width, height)
    timed_out = threading.Event()

    # stderr 落临时文件而非 PIPE：PIPE 没人读，写满即死锁（`-loglevel error` 只是让它
    # 不容易写满，不是保证）。落文件后失败时还能把 ffmpeg 的原话带进异常，不必靠猜。
    with tempfile.TemporaryFile() as errf:
        with subprocess.Popen(
            cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=errf
        ) as proc:
            # 看门狗：坏盘/网络盘上 readinto 会无上限阻塞。kill 后 stdout 见 EOF →
            # 下面短读抛错。不在 BufferedReader 上混用 select（缓冲区里已有的数据
            # select 看不见）。
            def _on_timeout() -> None:
                timed_out.set()
                proc.kill()

            watchdog = threading.Timer(budget, _on_timeout)
            watchdog.daemon = True
            watchdog.start()
            try:
                for k in range(k_start, k_end + 1):
                    # 每帧新建 buffer：np.frombuffer 与它共享内存，上一帧已经交给调用方，
                    # 复用会就地改写别人手里的像素。顺带让返回的帧**可写**（下游 cv2
                    # 原地操作要），np.frombuffer 出来的数组本身是只读的。
                    buf = bytearray(frame_size)
                    if not _read_exact(proc.stdout, buf):
                        raise RuntimeError(
                            _decode_failure(ref, k, k_start, k_end, timed_out, budget, errf)
                        )
                    yield Frame(
                        timestamp=float(sidecar[k]),
                        frame=np.frombuffer(buf, np.uint8).reshape((height, width, 3)),
                    )
            finally:
                watchdog.cancel()
                proc.kill()
                proc.wait()


def read_segment(
    task_id: int,
    step_id: int,
    ref: SegmentRef,
    *,
    width: int,
    height: int,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> Iterator[Frame]:
    """单段解码 —— `insert_segment` 的逆运算。帧级裁剪到 `[start_ts, end_ts]`。

    Args:
        task_id: 任务 id。
        step_id: 洗消步骤 id。
        ref: 段身份键。`ref.track` 必须是 `"raw"`。
        width / height: 输出分辨率。**无默认值**——漏传该是 `TypeError`，不是静默产出
            一个尺寸，那会在下游变成 train-serve skew（规范设计约束 5）。
        start_ts / end_ts: 闭区间的墙钟秒，`None` 为该侧不设限。

    Returns:
        帧迭代器，按段内顺序。`timestamp` **位级等于**写入时的 `frame.timestamp`
        （sidecar 存的是 float64 原值），可直接与内存里的 ts 做 `==`。

    Raises:
        ValueError: `ref.track` 不是 raw。
        RuntimeError: 解码短读或超时（消息带 ffmpeg stderr 尾部）。
        FileNotFoundError | OSError: 机器上没有 ffmpeg / 起不了子进程。

    **缺 sidecar 返回空迭代器而不是抛**：段刚落盘 sidecar 尚未就位、或写侧那次
    best-effort 写失败了，都是读侧要照常走下去的情形（跳过该段，别让一个辅助索引
    打断整条迭代）。这与 `_idx.read` 的契约同源。

    **参数校验是即时的，不是等到迭代**：本函数自己不是生成器，算完裁剪边界后把
    `_run_ffmpeg` 的生成器返回出去。写错轨道在调用那一行就炸，不会潜伏到循环深处。
    """
    _require_raw(ref.track)

    sidecar = _idx.read(_layout.sidecar_path(task_id, step_id, ref))
    if len(sidecar) == 0:
        logger.warning(
            "[storage.hls] sidecar 缺失或为空，跳过该段: task_id=%s step_id=%s %s",
            task_id, step_id, _layout.segment_name(ref),
        )
        return iter(())

    # searchsorted 的返回天然自洽：k_start ∈ [0, len]、k_end ∈ [-1, len-1]，空区间
    # 一律落到 k_start > k_end。刻意不 clamp —— 把 k_end = -1（end_ts 早于本段首帧）
    # 救成 0 会把空区间误判成命中第 0 帧。
    k_start = 0 if start_ts is None else int(np.searchsorted(sidecar, start_ts, side="left"))
    k_end = (
        len(sidecar) - 1
        if end_ts is None
        else int(np.searchsorted(sidecar, end_ts, side="right")) - 1
    )
    if k_start > k_end:
        return iter(())

    return _run_ffmpeg(task_id, step_id, ref, sidecar, k_start, k_end, width, height)


def iter_frames(
    task_id: int,
    step_id: int,
    *,
    width: int,
    height: int,
    start_ts: Optional[float] = None,
    end_ts: Optional[float] = None,
) -> Iterator[Frame]:
    """跨段流式取帧：段级裁剪后逐段 `read_segment`，按时序拼成一条。

    **无 `track` 参数，恒为 raw**（见模块 docstring）。内存占用是 O(1 帧)——每段起一次
    ffmpeg、边解边出；要「逐段整段进内存」请自己走 `list_segments` + `read_segment`，
    一段多大是调用方的策略，不是本层的。

    `start_ts` / `end_ts` 为 `None` 表示该侧不设限，原样下传给帧级裁剪——**不能拿段起始
    数组的首尾当时间轴首尾**：那是段**起始** ts，末段的段首之后还有整整一段的帧。

    Raises: 同 `read_segment`（首次迭代时才发生，本函数是生成器）。
    """
    segs = _layout.list_segments(task_id, step_id, _RAW_TRACK)
    if not segs:
        return

    starts = np.array([ref.ts_us for ref in segs], dtype=np.float64)

    # 要找的是**包含** start_ts 的那一段，故 'right' - 1：'left' 取到的是 start_ts
    # **之后**的段。且段文件名的 ts_us 是截断值（`_layout.ts_to_us`），「start_ts 恰为
    # 该段首帧」时 start_ts*1e6 > ts_us，'left' 同样会跳过该段 —— 即不存在「大部分
    # 情况下对」，是无条件错。
    lo = (
        0
        if start_ts is None
        else max(0, int(np.searchsorted(starts, start_ts * 1e6, side="right")) - 1)
    )
    hi = (
        len(segs) - 1
        if end_ts is None
        else int(np.searchsorted(starts, end_ts * 1e6, side="right")) - 1
    )
    if lo > hi:  # end_ts 早于首段起点时 hi = -1，在此被拦下
        return

    for ref in segs[lo : hi + 1]:
        yield from read_segment(
            task_id, step_id, ref,
            width=width, height=height, start_ts=start_ts, end_ts=end_ts,
        )
