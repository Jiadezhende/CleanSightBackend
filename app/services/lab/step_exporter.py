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
1. hls.list_playable_segments 一次拿到"哪些段能播"与"各自多长"（EXTINF 真值，在途段已滤）
2. render_vod 拼 VOD 清单（EXT-X-MAP 引 init.mp4 + 段列表 + ENDLIST）
3. 清单落在 `{step}/hls/`（段与 init 的所在目录），相对 URI 才解析得到它们，喂 ffmpeg HLS demuxer
4. `-c copy -movflags +faststart` 输出到 temp_root

为什么不用 `-f concat`：段是 fMP4 fragment（无 moov），concat demuxer 单独 demux 时找不到
codec init 会失败。HLS demuxer 通过 EXT-X-MAP 先吃 init.mp4 再串 fragment，才能正确还原。

为什么必须自己补 `#EXT-X-ENDLIST`：写入侧 playlist 是 LIVE 形态（不写 ENDLIST），
ffmpeg 会当直播流只从 live edge（末尾几段）开始读，前面全丢。

依赖：ffmpeg 由 settings.ffmpeg_path 提供（项目自包含 .ffmpeg/bin/，见 app/settings.py）
"""

from __future__ import annotations

import logging
import secrets
import subprocess
import time
from pathlib import Path
from typing import Optional

from app.services.utils.vod_playlist import VodEntry, render_vod
from app.storage import hls

logger = logging.getLogger(__name__)


# 孤儿产物回收阈值：客户端中途断开时 Starlette 的 BackgroundTask 不保证跑到，
# 需要这层兜底。`.lab_exports` 不在 StorageCleanupWorker 的扫描范围内 —— 它只认**两级都是
# 十进制数字**的 `{task}/{step}/` 目录，`.lab_exports` 这个名字直接被跳过。
# ⚠ 这是**显式依赖**：TTL 判据已换成目录 mtime（不再看 metadata.json），谁把这个临时目录
# 改成数字名，它就会在 cleanup_days 后被当成过期 step 整个删掉，而这边不会有任何提示。
_ORPHAN_TTL_SECONDS = 30 * 60

# ffmpeg 超时按段数估。remux 是磁盘速度（无解码编码），5s/段 是很宽的余量。
_TIMEOUT_PER_SEGMENT_S = 5
_TIMEOUT_FLOOR_S = 120


class StepExportError(Exception):
    """StepExporter 通用错误基类。"""


class StepExportNoSegments(StepExportError):
    """该 (task_id, step_id, track) 没有可导出的段（无段 / 全是在途段）。"""


class StepExportInitMissing(StepExportError):
    """step 目录缺 `{track}_init.mp4`，fMP4 fragment 无法解码。"""


class StepExporter:
    """整 step 导出器。"""

    def __init__(
        self,
        ffmpeg_bin: Optional[str] = None,
        temp_root: Optional[Path] = None,
    ):
        """
        Args:
            ffmpeg_bin: ffmpeg 可执行文件路径；默认 None = 用项目自包含的 settings.ffmpeg_path
                （.ffmpeg/bin/，不回退 PATH），与后端 / ClipBuilder 同源。显式传参可覆写。
            temp_root: 产物输出根目录；不传则用 {storage_base_dir}/.lab_exports（与 ClipBuilder 同目录）
        """
        from app.settings import settings

        if ffmpeg_bin is None:
            ffmpeg_bin = settings.ffmpeg_path
        self._ffmpeg = ffmpeg_bin

        if temp_root is None:
            temp_root = settings.storage_base_dir / ".lab_exports"
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

        # 一次扫盘同时回答"哪些段能播"与"各自多长"：EXTINF 是时长唯一真值（不能用文件名
        # ts 差重推），而清单的键集合就是"已完成转码并登记"的判据——不在其中的是在途段
        # （mp4v 已落、transcode+append 未完成），喂给 ffmpeg 会静默截短。
        playable = hls.list_playable_segments(task_id, step_id, track)
        if not playable:
            # 两档文案的区分：盘上一个段都没有 vs 有段但一个都没登记进清单。
            if not hls.list_segments(task_id, step_id, track):
                raise StepExportNoSegments(
                    f"No {track} segments for task_id={task_id}, step_id={step_id}"
                )
            raise StepExportNoSegments(
                f"No playable {track} segments yet for task_id={task_id}, "
                f"step_id={step_id} (all in-flight or playlist missing)"
            )

        init_path = hls.init_path(task_id, step_id, track)
        if not init_path.exists():
            # 与 traceback._build_vod_playlist 同一判据：缺 init = 旧格式产物（不支持、
            # 无迁移路径）或首段仍在 transcode。两者都不可自愈。
            raise StepExportInitMissing(
                f"{hls.init_name(track)} not found for task {task_id} step {step_id}. "
                "This step is either mid-transcode or written in an unsupported "
                "legacy layout; it cannot be exported."
            )

        # 临时清单落在 init 与段的所在目录（`{step}/hls/`），裸文件名的相对 URI 才解析得到
        # 它们。`.export_*.m3u8` 匹配不上域内的段名 / init 名正则，读侧枚举天然跳过。
        hls_dir = init_path.parent
        nonce = secrets.token_hex(6)
        tmp_m3u8 = hls_dir / f".export_{nonce}.m3u8"
        output_path = self._temp_root / f"step_{task_id}_{step_id}_{track}_{nonce}.mp4"

        entries = [
            VodEntry(hls.segment_name(s.ref), s.duration_s) for s in playable
        ]
        tmp_m3u8.write_text(
            render_vod(entries, map_uri=hls.init_name(track)), encoding="utf-8"
        )
        try:
            self._run_ffmpeg(tmp_m3u8, output_path, n_segments=len(playable))
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
            task_id, step_id, track, len(playable), size_bytes / 1024 / 1024,
        )
        return output_path

    # -------- internal --------

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
