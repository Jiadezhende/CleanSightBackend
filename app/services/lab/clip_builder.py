"""ClipBuilder —— 从 raw 段拼出一个 ms 精度的 mp4，供送标。

    spec = ClipSpec(task_id, step_id, start_media_ms, end_media_ms)
    res  = ClipBuilder().build_one(spec, job_dir)   # res.output_path / res.start_ms

**区间收媒体刻度、产出带回绝对墙钟**：浏览器算不出真墙钟（媒体轴是压紧的），换算在后端做。
理由见 `docs/update/20260919_VIDEO_TIMEBASE_SELECTION.md` §5.3。

三条会**静默出错**的约束，每条都踩过：

- **段只从清单来**（`hls.list_segments`）。喂未登记的段给 ffmpeg 会 exit 0、
  无任何日志、产出少一截的 mp4，见 `docs/kb/DESIGN_SEGMENT_CONCAT.md` §5.3。
- **不用 `-f concat`**：fMP4 fragment 无 moov，单独 demux 解不出 codec init（同上文 §2）。
- **`-ss` 必须在 `-i` 之后**（输出侧 seek）。挪到输入侧 exit 0 产出 261 字节零流空壳。

临时清单的 EXTINF 直接用清单真值，**别改写成"相邻段 ts 差"**——实测对 ffmpeg 是空操作。

依赖：ffmpeg 由 `settings.ffmpeg_path` 提供（项目自包含 `.ffmpeg/bin/`）。
"""

from __future__ import annotations

import logging
import secrets
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Tuple

from app.services.utils.media_timeline import GAP_THRESHOLD_MS, MediaTimeline
from app.services.utils.vod_playlist import VodEntry, render_vod
from app.storage import hls

logger = logging.getLogger(__name__)

# 送标只吃 raw：processed 是渲染结果，标注要的是原始画面。
RAW_TRACK = "raw"


# ---------------------------------------------------------------------------
# 对外数据结构
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClipSpec:
    """一个待导出的视频段，区间用媒体刻度表达（理由见模块 docstring）。

    Attributes:
        task_id: 任务 id
        step_id: 洗消步骤 id
        start_media_ms: 区间起点，相对该 step raw 轨媒体轴原点的毫秒（= `currentTime × 1000`）
        end_media_ms: 区间终点，必须 > `start_media_ms`
        label: 透传到 Label Studio task meta 的可选标签（非 LS 内的 annotation label）
    """

    task_id: int
    step_id: int
    start_media_ms: int
    end_media_ms: int
    label: Optional[str] = None

    @property
    def duration_ms(self) -> int:
        return max(0, self.end_media_ms - self.start_media_ms)


@dataclass(frozen=True)
class ClipResult:
    """一个 `ClipSpec` 的产出。

    `start_ms` / `end_ms` 是后端由清单换算出的**绝对墙钟**：落库与 LS 元数据要回答的是
    "这段素材是几点拍的"，媒体刻度回答不了。
    """

    spec: ClipSpec
    output_path: Path
    start_ms: int
    end_ms: int
    duration_ms: int
    size_bytes: int
    n_source_segments: int


class ClipBuildError(Exception):
    """ClipBuilder 通用错误基类。"""


class ClipRangeOutOfBoundsError(ClipBuildError):
    """目标媒体区间整个落在该 step 的媒体轴之外，一个段都没选中。"""


class ClipRangeGapError(ClipBuildError):
    """选中的相邻段之间有真实录制停顿；跨越它的区间不产出成片。"""


# ---------------------------------------------------------------------------
# 主类
# ---------------------------------------------------------------------------


class ClipBuilder:
    """从 raw 轨拼出 ms 精度 mp4。"""

    def __init__(
        self,
        ffmpeg_bin: Optional[str] = None,
        temp_root: Optional[Path] = None,
        preset: str = "veryfast",
        max_duration_ms: int = 300_000,
    ):
        """
        Args:
            ffmpeg_bin: ffmpeg 路径；`None` = 用项目自包含的 `settings.ffmpeg_path`
                （`.ffmpeg/bin/`，**不回退 PATH**），与后端 / 集成测试同源。显式传参可覆写。
            temp_root: 临时输出根目录；不传则用 `{storage_base_dir}/.lab_exports`
            preset: libx264 preset
            max_duration_ms: 单段时长上限（兜底防御；上层路由也会拒绝）
        """
        from app.settings import settings

        self._ffmpeg = ffmpeg_bin if ffmpeg_bin is not None else settings.ffmpeg_path
        self._preset = preset
        self._max_duration_ms = int(max_duration_ms)

        if temp_root is None:
            temp_root = settings.storage_base_dir / ".lab_exports"
        self._temp_root = Path(temp_root)
        self._temp_root.mkdir(parents=True, exist_ok=True)

    # -------- public API --------

    def new_job_dir(self) -> Path:
        """为本次提交的全部产物建一个 nonce 子目录。"""
        job_dir = self._temp_root / secrets.token_hex(6)
        job_dir.mkdir(parents=True, exist_ok=False)
        return job_dir

    def build_one(self, spec: ClipSpec, job_dir: Path) -> ClipResult:
        """生成单个 clip 的 mp4。

        Raises:
            ClipRangeOutOfBoundsError: 区间与任何段都不相交
            ClipRangeGapError: 区间跨越真实录制停顿
            ClipBuildError: 入参非法 / 缺 init / ffmpeg 失败、超时、未找到
        """
        if spec.end_media_ms <= spec.start_media_ms:
            raise ClipBuildError(
                f"Invalid range: end_media_ms={spec.end_media_ms} "
                f"<= start_media_ms={spec.start_media_ms}"
            )
        if spec.duration_ms > self._max_duration_ms:
            raise ClipBuildError(
                f"Clip duration {spec.duration_ms} ms exceeds max {self._max_duration_ms} ms"
            )

        window = MediaTimeline.load(spec.task_id, spec.step_id, RAW_TRACK).select(
            spec.start_media_ms, spec.end_media_ms
        )
        if not window:
            raise ClipRangeOutOfBoundsError(
                f"No {RAW_TRACK} segments overlap media range "
                f"[{spec.start_media_ms}, {spec.end_media_ms}) ms "
                f"for task_id={spec.task_id}, step_id={spec.step_id}"
            )

        gap = window.first_gap()
        if gap is not None:
            cur, nxt, gap_ms = gap
            raise ClipRangeGapError(
                f"Recording gap of {gap_ms / 1000:.2f}s between "
                f"{hls.segment_name(cur.seg.ref)} and {hls.segment_name(nxt.seg.ref)} "
                f"(threshold {GAP_THRESHOLD_MS / 1000:.2f}s); a clip spanning it would "
                f"claim continuous footage that does not exist"
            )

        start_ms = window.wall_ms_at(spec.start_media_ms)
        end_ms = window.wall_ms_at(spec.end_media_ms)
        # 区间尾越过轨尾时 `wall_ms_at` 会贴到末段段尾，产物也就只有那么长。`duration_ms`
        # 必须跟着收——否则落库与 LS 元数据里的时长比真实 mp4 长（`end_ms − start_ms`
        # 与 `duration_ms` 对不上）。UI 侧越不过 `<video>.duration`，直调 API 容易踩。
        duration_ms = min(spec.duration_ms, max(0, end_ms - start_ms))

        output_path = job_dir / f"clip_{start_ms}_{end_ms}.mp4"
        self._run_ffmpeg(spec, window, output_path)

        try:
            size_bytes = output_path.stat().st_size
        except OSError as e:
            raise ClipBuildError(
                f"Output file missing after ffmpeg: {output_path} ({e})"
            ) from e

        return ClipResult(
            spec=spec,
            output_path=output_path,
            start_ms=start_ms,
            end_ms=end_ms,
            duration_ms=duration_ms,
            size_bytes=size_bytes,
            n_source_segments=len(window),
        )

    def build_all(
        self, specs: List[ClipSpec]
    ) -> Tuple[Path, List[Tuple[ClipSpec, Optional[ClipResult], Optional[Exception]]]]:
        """批量构建。单个失败不影响其它。

        Returns:
            `(job_dir, [(spec, result_or_None, exception_or_None)])`
        """
        job_dir = self.new_job_dir()
        outcomes: List[Tuple[ClipSpec, Optional[ClipResult], Optional[Exception]]] = []
        for spec in specs:
            try:
                outcomes.append((spec, self.build_one(spec, job_dir), None))
            except ClipBuildError as e:
                logger.warning(
                    "[Lab] ClipBuilder failed for task=%s step=%s media[%d,%d]: %s",
                    spec.task_id, spec.step_id,
                    spec.start_media_ms, spec.end_media_ms, e,
                )
                outcomes.append((spec, None, e))
        return job_dir, outcomes

    def cleanup(self, job_dir: Path) -> None:
        """删掉一个 job_dir 及其全部产物。失败只 warning（清理失败不该让提交结果翻车）。"""
        try:
            if job_dir.exists() and job_dir.is_dir():
                shutil.rmtree(job_dir)
        except OSError as e:
            logger.warning("[Lab] Failed to cleanup job_dir=%s: %s", job_dir, e)

    # -------- internal --------

    def _run_ffmpeg(
        self, spec: ClipSpec, window: MediaTimeline, output_path: Path
    ) -> None:
        """写临时清单 → 跑一次 HLS 拼接 + 输出侧精确裁剪。"""
        offset_ms = window.media_offset_ms(spec.start_media_ms)
        if offset_ms < 0:
            # select() 的相交判据保证首段覆盖 start，理论上到不了这里；真到了宁可多裁一点
            logger.warning(
                "[Lab] start_media_ms=%d 早于首个选中段；clamp offset 到 0", spec.start_media_ms
            )
            offset_ms = 0
        offset_s = offset_ms / 1000.0
        duration_s = spec.duration_ms / 1000.0

        init_path = hls.init_path(spec.task_id, spec.step_id, RAW_TRACK)
        step_dir = init_path.parent
        if not init_path.exists():
            raise ClipBuildError(
                f"{hls.init_name(RAW_TRACK)} missing in step dir: {step_dir} "
                f"(fMP4 fragment 段需要 EXT-X-MAP 才能解码)"
            )

        # 临时清单必须落在段与 init 所在的目录：条目是裸文件名，HLS demuxer 按**清单自身所在
        # 目录**解析。`.clip_*.m3u8` 匹配不上域内的段名 / init 名正则，读侧枚举天然跳过。
        tmp_m3u8 = step_dir / f".clip_{secrets.token_hex(4)}.m3u8"
        tmp_m3u8.write_text(
            render_vod(
                [VodEntry(hls.segment_name(p.seg.ref), p.seg.duration_s) for p in window],
                map_uri=hls.init_name(RAW_TRACK),
            ),
            encoding="utf-8",
        )

        cmd = [
            self._ffmpeg,
            "-y", "-loglevel", "error",
            "-allowed_extensions", "ALL",
            "-i", str(tmp_m3u8),
            # -ss/-to 在 -i 之后 = 输出侧 seek，别挪到前面（见模块 docstring）
            "-ss", f"{offset_s:.3f}",
            "-to", f"{offset_s + duration_s:.3f}",
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
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
            if result.returncode != 0:
                raise ClipBuildError(
                    f"ffmpeg failed (exit={result.returncode}): "
                    f"{(result.stderr or '')[-1500:]}"
                )
        except subprocess.TimeoutExpired as e:
            raise ClipBuildError(
                f"ffmpeg timeout after {timeout}s for clip "
                f"media[{spec.start_media_ms},{spec.end_media_ms}]"
            ) from e
        except FileNotFoundError as e:
            raise ClipBuildError(f"ffmpeg binary not found: {self._ffmpeg}") from e
        finally:
            tmp_m3u8.unlink(missing_ok=True)
