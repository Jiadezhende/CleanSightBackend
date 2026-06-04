"""
ClipBuilder — 从 raw 段拼接出 ms 精度的 mp4 clip。

输入：(task_id, step_id, start_ms, end_ms)
输出：单个 mp4 文件，时长 ≈ end_ms - start_ms

实现思路：
1. 复用 SegmentFinder.list_segments(track='raw') 拿到该 step 的全部 raw 段（按 ts_us 升序）
2. 过滤出与 [start_ms, end_ms] 时间区间重叠的段
3. 在 step 目录写一个临时 m3u8（EXT-X-MAP 引 init.mp4 + 选中段列表），喂给 ffmpeg HLS demuxer
4. 输出端用 -ss/-to 精确裁剪（重编码 libx264，关键帧无关）

为什么不用 `-f concat`：raw 段是 fMP4 fragment（无 moov），concat demuxer 单独 demux 时
找不到 codec init 会失败。HLS demuxer 通过 EXT-X-MAP 先吃 init.mp4 再串 fragment，能正确
还原拼接流。

依赖：ffmpeg 由 settings.ffmpeg_path 提供（项目自包含 .ffmpeg/bin/，见 app/settings.py）
"""

from __future__ import annotations

import logging
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from app.services.traceback.segment_finder import (
    SegmentFinder,
    SegmentRef,
    get_default_base_dir,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipSpec:
    """一个待导出的视频段。

    Attributes:
        task_id: 任务 id
        step_id: 洗消步骤 id
        start_ms: 区间起点（绝对墙钟 ms，与 segment ts_ms 同时基）
        end_ms: 区间终点（绝对墙钟 ms，必须 > start_ms）
        label: 透传到 Label Studio task meta 的可选标签（非 LS 内的 annotation label）
    """

    task_id: int
    step_id: int
    start_ms: int
    end_ms: int
    label: Optional[str] = None

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_ms - self.start_ms)


@dataclass(frozen=True)
class ClipResult:
    """一个 ClipSpec 的产出结果。"""

    spec: ClipSpec
    output_path: Path
    duration_ms: int
    size_bytes: int
    n_source_segments: int


# ---------------------------------------------------------------------------
# 异常
# ---------------------------------------------------------------------------


class ClipBuildError(Exception):
    """ClipBuilder 通用错误基类。"""


class ClipRangeOutOfBoundsError(ClipBuildError):
    """[start_ms, end_ms] 与该 step 的任何段都不重叠。"""


class ClipRangeGapError(ClipBuildError):
    """重叠段之间存在大于 gap_tolerance_ms 的时间空隙。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------


def _est_segment_duration_us(segs: List[SegmentRef], default_us: int) -> int:
    """估算一个「典型段」的微秒时长。

    用相邻段的 ts_us 中位差；没有相邻关系时 fallback 到 default_us。
    用于判断给定 end_ms 是否真的超过了「最后一段的结束位置」。
    """
    if len(segs) < 2:
        return default_us
    diffs = sorted(segs[i + 1].ts_us - segs[i].ts_us for i in range(len(segs) - 1))
    return diffs[len(diffs) // 2] or default_us


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class ClipBuilder:
    """从 raw 轨拼出 ms 精度 mp4。"""

    def __init__(
        self,
        finder: Optional[SegmentFinder] = None,
        ffmpeg_bin: str = "ffmpeg",
        temp_root: Optional[Path] = None,
        preset: str = "veryfast",
        max_duration_ms: int = 300_000,
        gap_tolerance_ms: int = 500,
        default_segment_duration_s: float = 10.0,
    ):
        """
        Args:
            finder: 段定位器；不传则用 get_default_base_dir() 构造
            ffmpeg_bin: ffmpeg 可执行文件路径（默认 PATH 上的 "ffmpeg"）
            temp_root: 临时输出根目录；不传则用 {base_dir}/.lab_exports
            preset: libx264 preset
            max_duration_ms: 单段时长上限（兜底防御；上层路由也会拒绝）
            gap_tolerance_ms: 相邻段时间戳间隔容忍上限（超过则视为段间空隙）
            default_segment_duration_s: 段时长 fallback（用于估计 last seg 是否覆盖 end_ms）
        """
        self._finder = finder or SegmentFinder(get_default_base_dir())
        self._ffmpeg = ffmpeg_bin
        self._preset = preset
        self._max_duration_ms = int(max_duration_ms)
        self._gap_tolerance_ms = int(gap_tolerance_ms)
        self._default_seg_dur_us = int(default_segment_duration_s * 1_000_000)

        if temp_root is None:
            temp_root = self._finder.base_dir / ".lab_exports"
        self._temp_root = Path(temp_root)
        self._temp_root.mkdir(parents=True, exist_ok=True)

    # -------- public API --------

    def new_job_dir(self) -> Path:
        """创建一个新的 nonce 子目录用于本次提交的产物。"""
        nonce = secrets.token_hex(6)
        job_dir = self._temp_root / nonce
        job_dir.mkdir(parents=True, exist_ok=False)
        return job_dir

    def build_one(self, spec: ClipSpec, job_dir: Path) -> ClipResult:
        """生成单个 clip 的 mp4。失败抛 ClipBuildError 子类。"""
        if spec.end_ms <= spec.start_ms:
            raise ClipBuildError(
                f"Invalid range: end_ms={spec.end_ms} <= start_ms={spec.start_ms}"
            )
        if spec.duration_ms > self._max_duration_ms:
            raise ClipBuildError(
                f"Clip duration {spec.duration_ms} ms exceeds max {self._max_duration_ms} ms"
            )

        segs = self._select_segments(spec)
        if not segs:
            raise ClipRangeOutOfBoundsError(
                f"No raw segments overlap [start_ms={spec.start_ms}, end_ms={spec.end_ms}] "
                f"for task_id={spec.task_id}, step_id={spec.step_id}"
            )
        self._validate_continuity(segs)

        output_path = job_dir / f"clip_{spec.start_ms}_{spec.end_ms}.mp4"
        self._run_ffmpeg(spec, segs, output_path)

        try:
            size_bytes = output_path.stat().st_size
        except OSError as e:
            raise ClipBuildError(
                f"Output file missing after ffmpeg: {output_path} ({e})"
            ) from e

        return ClipResult(
            spec=spec,
            output_path=output_path,
            duration_ms=spec.duration_ms,
            size_bytes=size_bytes,
            n_source_segments=len(segs),
        )

    def build_all(
        self, specs: List[ClipSpec]
    ) -> Tuple[Path, List[Tuple[ClipSpec, Optional[ClipResult], Optional[Exception]]]]:
        """批量构建。

        Returns:
            (job_dir, [(spec, result_or_None, exception_or_None)])
            单个 clip 失败时其它继续；调用方根据各项 exception 自行处理。
        """
        job_dir = self.new_job_dir()
        outcomes: List[Tuple[ClipSpec, Optional[ClipResult], Optional[Exception]]] = []
        for spec in specs:
            try:
                res = self.build_one(spec, job_dir)
                outcomes.append((spec, res, None))
            except ClipBuildError as e:
                logger.warning(
                    "[Lab] ClipBuilder failed for task=%s step=%s [%d,%d]: %s",
                    spec.task_id, spec.step_id, spec.start_ms, spec.end_ms, e,
                )
                outcomes.append((spec, None, e))
        return job_dir, outcomes

    def cleanup(self, job_dir: Path) -> None:
        """删除一个 job_dir 及其全部产物。失败时只打 warning。"""
        try:
            if job_dir.exists() and job_dir.is_dir():
                shutil.rmtree(job_dir)
        except OSError as e:
            logger.warning("[Lab] Failed to cleanup job_dir=%s: %s", job_dir, e)

    # -------- internal --------

    def _select_segments(self, spec: ClipSpec) -> List[SegmentRef]:
        """返回与 [start_ms, end_ms] 重叠的 raw 段列表（按时间升序）。"""
        all_segs = self._finder.list_segments(spec.task_id, spec.step_id, "raw")
        if not all_segs:
            return []

        start_us = spec.start_ms * 1000
        end_us = spec.end_ms * 1000
        est_dur_us = _est_segment_duration_us(all_segs, self._default_seg_dur_us)

        overlapping: List[SegmentRef] = []
        for s in all_segs:
            seg_start_us = s.ts_us
            seg_end_us = s.ts_us + est_dur_us  # 估算的段尾
            # 标准区间相交：a_start < b_end AND a_end > b_start
            if seg_start_us < end_us and seg_end_us > start_us:
                overlapping.append(s)
        return overlapping

    def _validate_continuity(self, segs: List[SegmentRef]) -> None:
        """相邻段 ts_us 间隔不能超过 default_seg_dur_us + gap_tolerance_ms。"""
        if len(segs) < 2:
            return
        max_gap_us = self._default_seg_dur_us + self._gap_tolerance_ms * 1000
        for i in range(len(segs) - 1):
            gap_us = segs[i + 1].ts_us - segs[i].ts_us
            if gap_us > max_gap_us:
                raise ClipRangeGapError(
                    f"Gap {gap_us / 1_000_000:.2f}s between segments "
                    f"{segs[i].filename} and {segs[i + 1].filename} "
                    f"exceeds tolerance ({max_gap_us / 1_000_000:.2f}s)"
                )

    def _run_ffmpeg(
        self, spec: ClipSpec, segs: List[SegmentRef], output_path: Path
    ) -> None:
        """跑一次 ffmpeg HLS 拼接 + 精确裁剪。"""
        # offset 是相对于「拼接后流」的起点（即第一段的 ts_us）
        first_seg_ts_us = segs[0].ts_us
        offset_us = spec.start_ms * 1000 - first_seg_ts_us
        if offset_us < 0:
            logger.warning(
                "[Lab] start_ms=%d 早于第一段 ts_us=%d；clamp offset 到 0",
                spec.start_ms, first_seg_ts_us,
            )
            offset_us = 0
        offset_s = offset_us / 1_000_000.0
        duration_s = spec.duration_ms / 1000.0
        end_s = offset_s + duration_s

        step_dir = segs[0].path.parent
        init_path = step_dir / "init.mp4"
        if not init_path.exists():
            raise ClipBuildError(
                f"init.mp4 missing in step dir: {step_dir} "
                f"(fMP4 fragment 段需要 EXT-X-MAP 才能解码)"
            )

        # 临时 m3u8 落在 step 目录，让 EXT-X-MAP/EXTINF 的相对 URI 能解析到 init.mp4 与各段
        nonce = secrets.token_hex(4)
        tmp_m3u8 = step_dir / f".clip_{nonce}.m3u8"
        ext_inf_s = self._default_seg_dur_us / 1_000_000.0

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{int(ext_inf_s) + 1}",
            '#EXT-X-MAP:URI="init.mp4"',
        ]
        for s in segs:
            lines.append(f"#EXTINF:{ext_inf_s:.3f},")
            lines.append(s.path.name)
        lines.append("#EXT-X-ENDLIST")
        tmp_m3u8.write_text("\n".join(lines) + "\n", encoding="utf-8")

        cmd = [
            self._ffmpeg,
            "-y", "-loglevel", "error",
            "-allowed_extensions", "ALL",
            "-i", str(tmp_m3u8),
            "-ss", f"{offset_s:.3f}",
            "-to", f"{end_s:.3f}",
            "-c:v", "libx264",
            "-preset", self._preset,
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-an",
            "-movflags", "+faststart",
            "-f", "mp4",
            str(output_path),
        ]
        timeout = max(60, int(duration_s * 4))

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode != 0:
                raise ClipBuildError(
                    f"ffmpeg failed (exit={result.returncode}): "
                    f"{(result.stderr or '')[-1500:]}"
                )
        except subprocess.TimeoutExpired as e:
            raise ClipBuildError(
                f"ffmpeg timeout after {timeout}s for clip [{spec.start_ms},{spec.end_ms}]"
            ) from e
        except FileNotFoundError as e:
            raise ClipBuildError(
                f"ffmpeg binary not found: {self._ffmpeg}"
            ) from e
        finally:
            tmp_m3u8.unlink(missing_ok=True)
