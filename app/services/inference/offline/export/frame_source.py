"""帧源 —— 由 feature 的 ts 精确取回对应像素帧。

    ts → 定位 raw 段 → 读 sidecar 二分得 ordinal → 顺序解码该段取第 ordinal 帧

依赖 raw 段落盘时写的逐帧索引 sidecar（见 persistence/strategies/raw_frame_index.py）。
无 sidecar 一律判为**取不到**并计入统计，绝不退化成按 eff_fps 近似反推——实测那样会让
38.8% 的帧错位（database/99/2，428 帧样本），且错位事后无法归因。

两条硬约束，都来自实测：

1. **必须逐段解码，绝不能把多段拼成一条 playlist。** 实测 database/99/2 三段（300/300/257 帧）
   拼成一条 playlist 用 ffmpeg 解只出 600 帧——第三段被整段静默丢弃；逐段单独解码则
   300/300/257 全对。成因是段间时基/tfdt 不一致（属 HLS 时基修复那条线的既有问题），
   在它修好之前，拼接解码会静默丢帧，是正确性问题不是性能问题。
2. **按段流式产出，不一次性驻留全部像素。** 一条 10 分钟 step 的全量 BGR 帧是数百 MB
   （428 帧 @640×480 已经 394MB），必须解一段、算一段、只留降维后的特征。

fMP4 fragment 无 moov，**不能单独解码**：须写临时 m3u8（`EXT-X-MAP` 引 init.mp4）喂 HLS
demuxer，手法与 lab/clip_builder.py 同源。
"""

from __future__ import annotations

import bisect
import logging
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

import numpy as np

from app.services.persistence.strategies.raw_frame_index import read_frame_index
from app.services.traceback.segment_finder import SegmentFinder

logger = logging.getLogger(__name__)

_TS_EPS = 1e-6  # ts 同源同值，只为吸收 float 往返误差；不是"就近匹配"的容差


class FrameSourceError(Exception):
    """帧源不可用（init.mp4 缺失、ffmpeg 不可执行等），与"个别帧取不到"区分。"""


@dataclass
class FetchStats:
    """取帧过程的质量计数，逐项进 manifest。"""

    pixel_hit: int = 0
    no_sidecar: int = 0
    not_in_playlist: int = 0
    no_segment: int = 0
    decode_short: int = 0  # 段解码出的帧数少于 sidecar 记录 → 尾部若干 ordinal 取不到

    @property
    def pixel_miss(self) -> int:
        return self.no_sidecar + self.not_in_playlist + self.no_segment + self.decode_short


@dataclass
class _SegmentPlan:
    """一个段要取的帧：段内 ordinal → 输出序号。"""

    path: Path
    wanted: List[Tuple[int, int]] = field(default_factory=list)  # (ordinal, out_index)
    frame_count: int = 0


class FrameSource:
    """按 (task_id, step_id) + ts 列表取回像素帧，按段流式产出。"""

    def __init__(
        self,
        base_dir: Path,
        ffmpeg_bin: Optional[str] = None,
        timeout_s: int = 300,
    ):
        self._finder = SegmentFinder(Path(base_dir))
        if ffmpeg_bin is None:
            from app.settings import settings
            ffmpeg_bin = settings.ffmpeg_path
        self._ffmpeg = ffmpeg_bin
        self._timeout_s = int(timeout_s)

    # -------- public --------

    def iter_batches(
        self,
        task_id: int,
        step_id: int,
        ts_list: Sequence[float],
        width: int,
        height: int,
        stats: Optional[FetchStats] = None,
    ) -> Iterator[Tuple[List[int], np.ndarray]]:
        """按段产出 `(输出序号列表, 帧批 [n,H,W,3] BGR uint8)`。

        取不到的帧不出现在任何 batch 里——调用方据缺席把它们 mask 掉（不变式 F4：
        绝不用零向量/邻帧/插值冒充真实帧）。

        Args:
            task_id / step_id: 稳定存储键
            ts_list: 要取的帧 ts（= FrameFeature.ts，与 raw 帧 ts 同源同值）
            width / height: 源帧分辨率（rawvideo 管道需据此切帧）
            stats: 质量计数器；传入则原地累加
        """
        stats = stats if stats is not None else FetchStats()
        plans = self._plan(task_id, step_id, ts_list, stats)
        for plan in plans:
            if not plan.wanted:
                continue
            decoded = self._decode_segment(plan.path, width, height)
            if decoded is None:
                stats.decode_short += len(plan.wanted)
                continue
            out_idx: List[int] = []
            picked: List[np.ndarray] = []
            for ordinal, oi in plan.wanted:
                if ordinal < len(decoded):
                    out_idx.append(oi)
                    picked.append(decoded[ordinal])
                else:  # 解码帧数少于 sidecar 记录（段尾损坏等）
                    stats.decode_short += 1
            if out_idx:
                stats.pixel_hit += len(out_idx)
                yield out_idx, np.stack(picked)

    # -------- internal --------

    def _plan(
        self,
        task_id: int,
        step_id: int,
        ts_list: Sequence[float],
        stats: FetchStats,
    ) -> List[_SegmentPlan]:
        """把 ts 列表分派到各段，并把取不到的原因分类计数。"""
        segs = self._finder.list_segments(task_id, step_id, "raw")
        if not segs:
            stats.no_segment += len(ts_list)
            return []

        step_dir = segs[0].path.parent
        if not (step_dir / "init.mp4").exists():
            raise FrameSourceError(
                f"init.mp4 缺失: {step_dir}（fMP4 fragment 段需 EXT-X-MAP 才能解码）"
            )
        in_playlist = self._playlist_members(step_dir)

        starts = [s.ts_us / 1_000_000.0 for s in segs]
        # 段级缓存：sidecar 只读一次；None 表示该段不可用（无索引/不在 playlist）
        index_cache: Dict[Path, Optional[List[float]]] = {}
        plans: Dict[Path, _SegmentPlan] = {}

        for out_index, ts in enumerate(ts_list):
            i = bisect.bisect_right(starts, ts) - 1
            if i < 0:
                stats.no_segment += 1
                continue
            seg = segs[i]
            if seg.path not in index_cache:
                if in_playlist is not None and seg.path.name not in in_playlist:
                    # 转码失败的段留在磁盘但不进 playlist，帧不可取
                    index_cache[seg.path] = None
                    logger.debug("[FrameSource] 段不在 playlist，跳过 %s", seg.path.name)
                else:
                    index_cache[seg.path] = read_frame_index(seg.path)

            frame_ts = index_cache[seg.path]
            if frame_ts is None:
                if in_playlist is not None and seg.path.name not in in_playlist:
                    stats.not_in_playlist += 1
                else:
                    stats.no_sidecar += 1
                continue

            ordinal = self._locate(frame_ts, ts)
            if ordinal is None:
                # ts 落在该段起点之后但不在其帧表里（帧已被队列淘汰 / 落在段间空隙）
                stats.no_segment += 1
                continue
            plan = plans.setdefault(
                seg.path, _SegmentPlan(path=seg.path, frame_count=len(frame_ts))
            )
            plan.wanted.append((ordinal, out_index))

        return [plans[p] for p in sorted(plans, key=lambda p: p.name)]

    @staticmethod
    def _locate(frame_ts: Sequence[float], ts: float) -> Optional[int]:
        """在有序帧 ts 表里二分定位 ordinal。**精确匹配**，不做就近取。"""
        j = bisect.bisect_left(frame_ts, ts - _TS_EPS)
        if j < len(frame_ts) and abs(frame_ts[j] - ts) <= _TS_EPS:
            return j
        return None

    @staticmethod
    def _playlist_members(step_dir: Path) -> Optional[set]:
        """raw_playlist.m3u8 里列出的段文件名；playlist 缺失返回 None（不做过滤）。"""
        playlist = step_dir / "raw_playlist.m3u8"
        if not playlist.exists():
            return None
        try:
            return {
                line.strip()
                for line in playlist.read_text(encoding="utf-8").splitlines()
                if line.strip() and not line.startswith("#")
            }
        except OSError as e:
            logger.warning("[FrameSource] playlist 读取失败 %s: %s", playlist, e)
            return None

    def _decode_segment(
        self, segment_path: Path, width: int, height: int
    ) -> Optional[np.ndarray]:
        """整段顺序解码为 [N,H,W,3] BGR uint8。失败返回 None。

        **一段一个临时 m3u8**——见模块 docstring：多段拼接会静默丢整段。
        """
        step_dir = segment_path.parent
        tmp_m3u8 = step_dir / f".export_{secrets.token_hex(4)}.m3u8"
        tmp_m3u8.write_text(
            "\n".join([
                "#EXTM3U",
                "#EXT-X-VERSION:7",
                "#EXT-X-PLAYLIST-TYPE:VOD",
                "#EXT-X-TARGETDURATION:60",
                '#EXT-X-MAP:URI="init.mp4"',
                "#EXTINF:60.0,",
                segment_path.name,
                "#EXT-X-ENDLIST",
            ]) + "\n",
            encoding="utf-8",
        )
        cmd = [
            self._ffmpeg,
            "-loglevel", "error",
            "-allowed_extensions", "ALL",
            "-i", str(tmp_m3u8),
            "-f", "rawvideo", "-pix_fmt", "bgr24",
            "-an", "-",
        ]
        try:
            proc = subprocess.run(
                cmd, capture_output=True, timeout=self._timeout_s, check=False
            )
            if proc.returncode != 0:
                logger.warning(
                    "[FrameSource] 解码失败 %s: %s",
                    segment_path.name, (proc.stderr or b"").decode(errors="replace")[-500:],
                )
                return None
            frame_bytes = width * height * 3
            n = len(proc.stdout) // frame_bytes
            if n == 0:
                logger.warning("[FrameSource] 解码出 0 帧 %s", segment_path.name)
                return None
            return np.frombuffer(proc.stdout[: n * frame_bytes], dtype=np.uint8).reshape(
                n, height, width, 3
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as e:
            logger.warning("[FrameSource] 解码异常 %s: %s", segment_path.name, e)
            return None
        finally:
            tmp_m3u8.unlink(missing_ok=True)
