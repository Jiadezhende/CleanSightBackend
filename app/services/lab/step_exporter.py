"""
StepExporter —— 把一个 (task_id, step_id, track) 的全部落盘段导出为单个 mp4。

输入：(task_id, step_id, track)
输出：单个 mp4 文件，内容 = 该 step 该轨已完成落盘的全部段按时序拼接

与 ClipBuilder 的分工：
- ClipBuilder：ms 精度区间裁剪 → 必须 -ss/-to + libx264 重编码（送标用）
- StepExporter：整段导出 → 纯 `-c copy` remux（汇报素材 / 取原片用）

之所以能 `-c copy`：段落盘时已由 hls_strategy 转成 H.264/yuv420p/CRF23 的 fMP4
fragment，remux 成 mp4 只是换容器——磁盘速度、零 CPU、零二次画质损失。

实现思路（与 ClipBuilder._run_ffmpeg 同构，坑点相同）：
1. SegmentFinder.list_segments 拿该 step 该轨全部段（按 ts_us 升序）
2. 用写入侧 playlist 的 EXTINF 作时长真值，同时过滤在途段
3. 在 step 目录写临时 m3u8（EXT-X-MAP 引 init.mp4 + 段列表 + ENDLIST），喂 ffmpeg HLS demuxer
4. `-c copy -movflags +faststart` 输出到 temp_root

为什么不用 `-f concat`：段是 fMP4 fragment（无 moov），concat demuxer 单独 demux 时找不到
codec init 会失败。HLS demuxer 通过 EXT-X-MAP 先吃 init.mp4 再串 fragment，才能正确还原。

为什么必须自己补 `#EXT-X-ENDLIST`：写入侧 playlist 是 LIVE 形态（不写 ENDLIST），
ffmpeg 会当直播流只从 live edge（末尾几段）开始读，前面全丢。

依赖：ffmpeg 由 settings.ffmpeg_path 提供（项目自包含 .ffmpeg/bin/，见 app/settings.py）
"""

from __future__ import annotations

import logging
import math
import secrets
import subprocess
import time
from pathlib import Path
from typing import List, Optional

from app.services.traceback.segment_finder import (
    SegmentFinder,
    SegmentRef,
    get_default_base_dir,
    parse_playlist_durations,
)

logger = logging.getLogger(__name__)


# 孤儿产物回收阈值：客户端中途断开时 Starlette 的 BackgroundTask 不保证跑到，
# 需要这层兜底。.lab_exports 不在 StorageCleanupWorker 的扫描范围内
# （它按 metadata.json 判 step 目录，非数字目录名被 _dir_name_to_int 跳过）。
_ORPHAN_TTL_SECONDS = 30 * 60

# ffmpeg 超时按段数估。remux 是磁盘速度（无解码编码），5s/段 是很宽的余量。
_TIMEOUT_PER_SEGMENT_S = 5
_TIMEOUT_FLOOR_S = 120


class StepExportError(Exception):
    """StepExporter 通用错误基类。"""


class StepExportNoSegments(StepExportError):
    """该 (task_id, step_id, track) 没有可导出的段（无段 / 全是在途段）。"""


class StepExportInitMissing(StepExportError):
    """step 目录缺 init.mp4，fMP4 fragment 无法解码。"""


class StepExporter:
    """整 step 导出器。"""

    def __init__(
        self,
        finder: Optional[SegmentFinder] = None,
        ffmpeg_bin: Optional[str] = None,
        temp_root: Optional[Path] = None,
    ):
        """
        Args:
            finder: 段定位器；不传则用 get_default_base_dir() 构造
            ffmpeg_bin: ffmpeg 可执行文件路径；默认 None = 用项目自包含的 settings.ffmpeg_path
                （.ffmpeg/bin/，不回退 PATH），与后端 / ClipBuilder 同源。显式传参可覆写。
            temp_root: 产物输出根目录；不传则用 {base_dir}/.lab_exports（与 ClipBuilder 同目录）
        """
        self._finder = finder or SegmentFinder(get_default_base_dir())
        if ffmpeg_bin is None:
            from app.settings import settings

            ffmpeg_bin = settings.ffmpeg_path
        self._ffmpeg = ffmpeg_bin

        if temp_root is None:
            temp_root = self._finder.base_dir / ".lab_exports"
        self._temp_root = Path(temp_root)
        self._temp_root.mkdir(parents=True, exist_ok=True)

    # -------- public API --------

    def export(self, task_id: int, step_id: int, track: str) -> Path:
        """导出整个 step 的指定轨为单个 mp4，返回产物路径。

        产物归调用方所有——用完须自行删除（路由层挂 BackgroundTask）。

        Raises:
            StepExportNoSegments: 无段 / 段全在途
            StepExportInitMissing: step 目录缺 init.mp4
            StepExportError: ffmpeg 失败 / 超时 / 未找到
        """
        self._sweep_orphans()

        segs = self._finder.list_segments(task_id, step_id, track)
        if not segs:
            raise StepExportNoSegments(
                f"No {track} segments for task_id={task_id}, step_id={step_id}"
            )

        step_dir = self._finder.task_dir(task_id, step_id)

        # EXTINF 是时长唯一真值（不能用文件名 ts 差重推）；键集合同时充当"已完成落盘"
        # 的判据——不在其中的是在途段（mp4v 已落、transcode+append 未完成），必须过滤，
        # 否则 fragment 实际媒体时长与 playlist 估算对不上。
        durations = parse_playlist_durations(step_dir / f"{track}_playlist.m3u8")
        segs = [s for s in segs if s.filename in durations]
        if not segs:
            raise StepExportNoSegments(
                f"No playable {track} segments yet for task_id={task_id}, "
                f"step_id={step_id} (all in-flight or playlist missing)"
            )

        if not (step_dir / f"{track}_init.mp4").exists():
            raise StepExportInitMissing(
                f"{track}_init.mp4 not found for task {task_id} step {step_id}. "
                "Historical segments must be migrated via "
                "scripts/transcode_segments_to_h264.py before export."
            )

        nonce = secrets.token_hex(6)
        tmp_m3u8 = step_dir / f".export_{nonce}.m3u8"
        output_path = self._temp_root / f"step_{task_id}_{step_id}_{track}_{nonce}.mp4"

        tmp_m3u8.write_text(
            self._build_vod_text(segs, durations, track), encoding="utf-8"
        )
        try:
            self._run_ffmpeg(tmp_m3u8, output_path, n_segments=len(segs))
        finally:
            tmp_m3u8.unlink(missing_ok=True)

        try:
            size_bytes = output_path.stat().st_size
        except OSError as e:
            raise StepExportError(
                f"Output file missing after ffmpeg: {output_path} ({e})"
            ) from e

        logger.info(
            "[Lab] step export done: task=%s step=%s track=%s segments=%d size=%.1fMB",
            task_id, step_id, track, len(segs), size_bytes / 1024 / 1024,
        )
        return output_path

    # -------- internal --------

    @staticmethod
    def _build_vod_text(segs: List[SegmentRef], durations: dict, track: str) -> str:
        """构造喂给 ffmpeg 的 VOD m3u8 文本。

        段用 basename 引用 —— 该 m3u8 落在 step 目录，相对 URI 才解析得到
        同目录的 init.mp4 与各段文件。

        调用方保证 segs 非空、已按时序升序、且每个 filename 都在 durations 里。
        """
        seg_durs = [durations[s.filename] for s in segs]
        target_duration = max(int(math.ceil(max(seg_durs))), 1)

        lines = [
            "#EXTM3U",
            "#EXT-X-VERSION:7",
            "#EXT-X-PLAYLIST-TYPE:VOD",
            f"#EXT-X-TARGETDURATION:{target_duration}",
            "#EXT-X-MEDIA-SEQUENCE:0",
            f'#EXT-X-MAP:URI="{track}_init.mp4"',
        ]
        for s, dur in zip(segs, seg_durs):
            lines.append(f"#EXTINF:{dur:.3f},")
            lines.append(s.path.name)
        # 缺 ENDLIST → ffmpeg 当直播流只读 live edge，前面的段全丢
        lines.append("#EXT-X-ENDLIST")
        return "\n".join(lines) + "\n"

    def _run_ffmpeg(self, m3u8_path: Path, output_path: Path, n_segments: int) -> None:
        """HLS demuxer 串 fragment → mp4 容器，纯 remux 不重编码。"""
        cmd = [
            self._ffmpeg,
            "-y", "-loglevel", "error",
            "-allowed_extensions", "ALL",
            "-i", str(m3u8_path),
            # 段本就是 H.264/yuv420p/CRF23（hls_strategy 落盘时已转），换容器即可：
            # 磁盘速度、零 CPU、零二次画质损失。
            "-c", "copy",
            # moov 前置，边下边播 / 拖动 seek 不用等整个文件
            "-movflags", "+faststart",
            "-f", "mp4",
            str(output_path),
        ]
        timeout = max(_TIMEOUT_FLOOR_S, n_segments * _TIMEOUT_PER_SEGMENT_S)

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            output_path.unlink(missing_ok=True)
            raise StepExportError(
                f"ffmpeg timeout after {timeout}s ({n_segments} segments)"
            ) from e
        except FileNotFoundError as e:
            raise StepExportError(f"ffmpeg binary not found: {self._ffmpeg}") from e

        if result.returncode != 0:
            output_path.unlink(missing_ok=True)
            raise StepExportError(
                f"ffmpeg failed (exit={result.returncode}): "
                f"{(result.stderr or '')[-1500:]}"
            )

    def _sweep_orphans(self) -> None:
        """删除 temp_root 下超过 TTL 的历史产物。失败只打 warning。"""
        cutoff = time.time() - _ORPHAN_TTL_SECONDS
        try:
            candidates = list(self._temp_root.glob("step_*.mp4"))
        except OSError as e:
            logger.warning("[Lab] sweep orphans failed to list %s: %s", self._temp_root, e)
            return
        for p in candidates:
            try:
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
                    logger.info("[Lab] swept orphan export: %s", p.name)
            except OSError as e:
                logger.warning("[Lab] sweep orphan failed %s: %s", p, e)
