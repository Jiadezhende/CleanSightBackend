"""
HLS 段 → 像素帧：step 目录落盘格式的读侧解码实现。

与写侧（`persistence/hls_strategy`）隔着一层 sidecar 契约互为逆运算：

    写侧  每落一段 mp4，同时落同名 .idx（该段每帧 frame.timestamp 的 float64 数组）
    读侧  按 .idx 下标 k 取段内第 k 帧

**「段内帧号 n ↔ sidecar 下标 k 严格 1:1」是整个索引的地基**，靠三件事保证：不拼 m3u8、
不用 `-ss`、`-vsync 0`。破其中任何一条都不报错，只会静默取到错帧。同理，`.idx` 的命名在
layout、内容解释在本模块，两者必须同居本包才能互相对照。

本模块只负责「给定 ts 区间 → 产出该区间的帧」，**不做 ts 匹配校验、不做单点筛选** —— 那是
消费侧的策略（`inference/offline/frame_finder.py`）。

**包内私有**：对外入口是 `Step.frames(track, start_ts, end_ts)`，它负责定位目录与段列表。
本类只吃已定位好的输入（一个 `Step` 句柄），不碰存储根、不自己定位。
"""

from __future__ import annotations

import bisect
import logging
import subprocess
import tempfile
import threading
from pathlib import Path
from typing import Optional, Iterator, Sequence
import numpy as np

from app.domain.frame import Frame
from app.services.step_store import layout
from app.services.step_store.store import SegmentRef, Step

logger = logging.getLogger(__name__)

# 单段解码的 ffmpeg 预算，口径同 step_exporter（max(下限, 规模 × 单位)）。
# 一段是有界工作量（≤ sidecar 条数），故按帧给预算而非给全局超时。
# 实测整段 150 帧解码 73ms，0.2s/帧 余量约 400×，只用于兜「坏盘/网络盘上永久阻塞」。
_DECODE_TIMEOUT_FLOOR_S = 30
_DECODE_TIMEOUT_PER_FRAME_S = 0.2

# 失败时带进异常的 ffmpeg stderr 尾部长度
_STDERR_TAIL_CHARS = 500


def _locate_containing_index(seg_ts_us: Sequence[int], target_us: float) -> int:
    """段起始 ts 升序数组中，**包含** target_us 的那一段的下标（最大的 i 满足
    `seg_ts_us[i] <= target_us`）；全都更晚时返回 -1。`target_us` 收浮点。

    ⚠ **必须是 `bisect_right - 1`，不能换 `bisect_left`**：文件名里的
    `ts_us = int(ts*1e6)` 是截断值，「target 恰为该段首帧」时 `target_us > ts_us`，left 会
    跳过该段。这是无条件错。

    **返回 -1 而不 clamp**：`iter()` 的两端对越界的处理相反（起点 clamp 到首段、终点保留
    -1 表达空区间），故不替调用方做决定。
    """
    return bisect.bisect_right(seg_ts_us, target_us) - 1


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


class SegmentDecoder:
    """ts → 段 → 段内帧号 → 像素。纯查询，零像素缓存，对外只有一个 `iter(start_ts, end_ts)`。

    解码走 `concat:{track}_init.mp4|{track}_segment_{ts}.mp4` + `select=between(n,k1,k2)`：
    段级裁剪省 ffmpeg 调用次数，帧级裁剪不存无效像素。
    """

    def __init__(
        self,
        step_dir: Path,
        track: str,
        segments: Sequence[SegmentRef],
        ffmpeg_bin: Optional[str] = None,
    ):
        """
        Args:
            step_dir: 已定位的 step 目录（由 `Step.frames` 给，本类不自己拼）
            track: 段与 init 属哪一轨。**init 名必须用它而非从段名反解** ——
                `raw_init.mp4` 解 processed 段会 SPS/PPS 不匹配
            segments: 该轨全部段，调用方保证已按 ts_us 升序
        """
        self._step_dir = Path(step_dir)
        self._track = track
        self._segs = list(segments)
        self._seg_ts_us = [seg.ts_us for seg in self._segs]
        if ffmpeg_bin is None:
            from app.settings import settings
            ffmpeg_bin = settings.ffmpeg_path
        self._ffmpeg_bin = ffmpeg_bin

    @classmethod
    def for_step(
        cls, step: "Step", track: str = "raw", ffmpeg_bin: Optional[str] = None
    ):
        """从 `Step` 句柄构造。**「解码要 Step 的哪几样」只写这一处** —— `Step.frames()`
        与测试的 seam 子类都经此构造，两边不会分叉。

        `playable_only=False`：解码只需要 mp4 与 sidecar 都在，与「该段有没有进 playlist」
        无关 —— 离线反查要能读到刚落盘、transcode 尚未 append 的段。
        """
        return cls(
            step_dir=step._dir,
            track=track,
            segments=step.segments(track, playable_only=False),
            ffmpeg_bin=ffmpeg_bin,
        )

    def iter(
        self,
        start_ts: Optional[float] = None,
        end_ts: Optional[float] = None,
        width: int = 640,
        height: int = 480,
    ) -> Iterator[Frame]:
        """段级裁剪，产出该时间范围内的所有 Frame。不做 ts 匹配校验、不做单点筛选。

        start_ts / end_ts 为 None 表示该侧不设限，原样下传给帧级裁剪 —— 不能拿
        `self._seg_ts_us` 的首尾当时间轴首尾：那是**段起始** ts，末段的段首之后还有整整
        一段的帧。
        """
        if not self._segs:
            return

        # 定位「包含该 ts 的段」的语义与 off-by-one 论证见 _locate_containing_index。
        # 两端对返回 -1 的处理相反：起点 clamp 到首段（区间左侧越界仍要从头出帧），
        # 终点保留 -1（end_ts 早于首段起点 = 空区间，clamp 成 0 会误出第 0 段）。
        lo = 0 if start_ts is None else max(
            0, _locate_containing_index(self._seg_ts_us, start_ts * 1e6)
        )
        hi = (
            len(self._segs) - 1
            if end_ts is None
            else _locate_containing_index(self._seg_ts_us, end_ts * 1e6)
        )
        if lo > hi:  # end_ts 早于首段起点时 hi = -1，在此被拦下
            return

        for seg in self._segs[lo:hi + 1]:
            yield from self._decode_segment(seg, start_ts, end_ts, width, height)

    def _decode_segment(
        self,
        seg: SegmentRef,
        start_ts: Optional[float],
        end_ts: Optional[float],
        width: int = 640,
        height: int = 480,
    ) -> Iterator[Frame]:
        """帧级裁剪，产出单个段内落在该时间范围的 Frame。"""
        sidecar = self._load_sidecar(seg)
        if len(sidecar) == 0:
            return

        # searchsorted 的返回天然自洽：k_start ∈ [0, len]、k_end ∈ [-1, len-1]，
        # 空区间一律落到 k_start > k_end。刻意不 clamp —— 把 k_end = -1
        #（end_ts 早于本段首帧）救成 0 会把空区间误判成命中第 0 帧。
        k_start = 0 if start_ts is None else int(
            np.searchsorted(sidecar, start_ts, side="left")
        )
        k_end = len(sidecar) - 1 if end_ts is None else int(
            np.searchsorted(sidecar, end_ts, side="right")
        ) - 1
        if k_start > k_end:
            return

        yield from self._run_ffmpeg(seg, sidecar, k_start, k_end, width, height)

    def _run_ffmpeg(
        self, seg: SegmentRef, sidecar: np.ndarray, k_start: int, k_end: int, width: int, height: int
    ) -> Iterator[Frame]:
        """解出段内 [k_start, k_end] 闭区间的帧，yield Frame。"""
        frame_size = width * height * 3
        n_frames = k_end - k_start + 1
        budget = max(_DECODE_TIMEOUT_FLOOR_S, n_frames * _DECODE_TIMEOUT_PER_FRAME_S)
        cmd = self._build_cmd(seg, k_start, k_end, width, height)
        timed_out = threading.Event()

        # stderr 落临时文件而非 PIPE：PIPE 没人读，写满即死锁（现状只是靠
        # -loglevel error 让它写不满）。落文件后失败时还能把 ffmpeg 的原话
        # 带进异常，不必靠猜。
        with tempfile.TemporaryFile() as errf:
            with subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=errf,
            ) as proc:
                # 看门狗：坏盘/网络盘上 readinto 会无上限阻塞。kill 后 stdout 见
                # EOF → 下面短读抛错。不在 BufferedReader 上混用 select（缓冲区
                # 里已有的数据 select 看不见）。
                def _on_timeout() -> None:
                    timed_out.set()
                    proc.kill()

                watchdog = threading.Timer(budget, _on_timeout)
                watchdog.daemon = True
                watchdog.start()
                try:
                    for k in range(k_start, k_end + 1):
                        # 每帧新建 buffer：np.frombuffer 与它共享内存，上一帧已经
                        # 交给调用方，复用会就地改写别人手里的像素。
                        buf = bytearray(frame_size)
                        if not _read_exact(proc.stdout, buf):
                            raise RuntimeError(
                                self._decode_failure(
                                    seg, k, k_start, k_end, timed_out, budget, errf
                                )
                            )
                        yield Frame(
                            timestamp=float(sidecar[k]),
                            frame=np.frombuffer(buf, np.uint8).reshape((height, width, 3)),
                        )
                finally:
                    watchdog.cancel()
                    proc.kill()
                    proc.wait()

    @staticmethod
    def _decode_failure(
        seg: SegmentRef,
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
        what = (
            f"ffmpeg timeout after {budget:g}s"
            if timed_out.is_set()
            else "Incomplete frame"
        )
        msg = f"{what} from {seg.filename} @ 段内帧 {k}（请求区间 [{k_start},{k_end}]）"
        return f"{msg}; ffmpeg stderr: {tail}" if tail else msg

    def _load_sidecar(self, seg: SegmentRef) -> np.ndarray:
        idx_path = self._step_dir / layout.sidecar_name_for(seg.filename)
        if not idx_path.exists():
            # 段刚落盘、sidecar 尚未就位，或历史遗留段：跳过该段，不打断整条迭代
            #（缺一段的索引不该让前后所有段一起读不了）。单点查询仍会在
            # FrameFinder.find 里因目标 ts 缺失而硬失败——**宽容留在这层、严格留在上层**。
            logger.warning("[SegmentDecoder] sidecar 缺失，跳过该段: %s", idx_path)
            return np.empty(0, dtype=np.float64)
        return np.fromfile(idx_path, dtype=np.float64)

    def _build_cmd(
        self, seg: SegmentRef, start: int, end: int, width: int, height: int
    ) -> list[str]:
        return [
            self._ffmpeg_bin,
            "-loglevel", "error", "-hide_banner",
            "-i", (
                f"concat:{self._step_dir / layout.init_name(self._track)}"
                f"|{self._step_dir / seg.filename}"
            ),
            # select 必须在 scale 之前：反过来会把注定被丢弃的帧也缩放一遍
            "-vf", f"select=between(n\\,{start}\\,{end}),scale={width}:{height}",
            "-vframes", str(end - start + 1),
            "-vsync", "0",
            "-f", "rawvideo",
            "-pix_fmt", "bgr24",
            "pipe:1",
        ]
