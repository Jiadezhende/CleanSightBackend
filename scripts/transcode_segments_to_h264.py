"""
批量转码历史 mp4 段为 H.264 + faststart（浏览器兼容）

背景：早期版本用 cv2.VideoWriter + fourcc("mp4v") 写出的 MPEG-4 Part 2 段，
浏览器 <video> 标签无法稳定播放，且 moov atom 在文件尾部。新版写入侧已自动
转码，但历史落盘文件仍是 mp4v。本脚本就地转换指定 task_id 下所有 mp4。

策略：
- 串行处理（避免占满 CPU），用 ffprobe 探测 codec_name 跳过已是 h264 的文件
- 转出到 .transcode.tmp 后原子替换原文件
- 单文件失败仅 WARN 并继续，不中断批次

用法：
    python -m scripts.transcode_segments_to_h264 1778293239052
    python -m scripts.transcode_segments_to_h264 1778293239052 1778293240000
    python -m scripts.transcode_segments_to_h264 --base-dir /data/db 1778293239052
    python -m scripts.transcode_segments_to_h264 --dry-run 1778293239052
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger("transcode_segments")


def _resolve_base_dir(cli_base_dir: Optional[str]) -> Path:
    if cli_base_dir:
        return Path(cli_base_dir).resolve()
    # 使用与 hls_strategy 写入侧、media 路由读取侧完全一致的解析逻辑
    from app.services.traceback.segment_finder import get_default_base_dir

    return get_default_base_dir()


def _resolve_ffmpeg() -> str:
    try:
        from app.settings import settings

        return settings.ffmpeg_path
    except Exception:
        return "ffmpeg"


def _probe_codec(ffprobe_bin: str, path: Path) -> Optional[str]:
    """返回 video stream 的 codec_name；ffprobe 失败返回 None。"""
    cmd = [
        ffprobe_bin,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=codec_name",
        "-of", "default=nw=1:nk=1",
        str(path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("ffprobe failed (%s): %s", type(e).__name__, path)
        return None
    if result.returncode != 0:
        logger.warning("ffprobe rc=%s: %s\nstderr: %s", result.returncode, path, result.stderr.strip())
        return None
    return result.stdout.strip() or None


def _transcode(ffmpeg_bin: str, path: Path) -> bool:
    """就地转码为 H.264 + faststart。成功返回 True。"""
    tmp_path = path.with_suffix(path.suffix + ".transcode.tmp")
    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel", "error",
        "-i", str(path),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-an",
        "-movflags", "+faststart",
        str(tmp_path),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("ffmpeg failed (%s): %s", type(e).__name__, path)
        tmp_path.unlink(missing_ok=True)
        return False

    if result.returncode != 0 or not tmp_path.exists():
        logger.warning("ffmpeg rc=%s: %s\nstderr: %s", result.returncode, path, result.stderr.strip())
        tmp_path.unlink(missing_ok=True)
        return False

    try:
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning("replace failed %s: %s", path, e)
        tmp_path.unlink(missing_ok=True)
        return False
    return True


def _collect_mp4s(task_dir: Path) -> List[Path]:
    """递归收集 task_dir 下所有 .mp4 文件（排除转码临时文件）。"""
    return sorted(
        p for p in task_dir.rglob("*.mp4")
        if not p.name.endswith(".transcode.tmp")
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task_ids", nargs="+", help="一个或多个 task_id（数字）")
    parser.add_argument("--base-dir", default=None, help="持久化 base_dir（默认读 persistence_config）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际转码")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg 二进制路径（默认读 settings.ffmpeg_path）")
    parser.add_argument("--ffprobe", default="ffprobe", help="ffprobe 二进制路径（默认 PATH 中的 ffprobe）")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    base_dir = _resolve_base_dir(args.base_dir)
    ffmpeg_bin = args.ffmpeg or _resolve_ffmpeg()
    ffprobe_bin = args.ffprobe

    if not base_dir.is_dir():
        logger.error("base_dir 不存在或不是目录: %s", base_dir)
        return 2

    logger.info("base_dir = %s", base_dir)
    logger.info("ffmpeg = %s, ffprobe = %s", ffmpeg_bin, ffprobe_bin)
    logger.info("task_ids = %s", args.task_ids)

    total = 0
    skipped_h264 = 0
    converted = 0
    failed = 0
    missing_tasks: List[str] = []

    for tid in args.task_ids:
        task_dir = base_dir / tid
        if not task_dir.is_dir():
            logger.warning("task 目录不存在，跳过: %s", task_dir)
            missing_tasks.append(tid)
            continue

        mp4s = _collect_mp4s(task_dir)
        logger.info("[task %s] 发现 %d 个 mp4 文件", tid, len(mp4s))
        for path in mp4s:
            total += 1
            codec = _probe_codec(ffprobe_bin, path)
            if codec == "h264":
                skipped_h264 += 1
                logger.info("[skip h264] %s", path.relative_to(base_dir))
                continue
            if codec is None:
                logger.warning("[probe fail] %s — 仍尝试转码", path.relative_to(base_dir))

            if args.dry_run:
                logger.info("[dry-run] would transcode (%s) %s", codec or "?", path.relative_to(base_dir))
                continue

            logger.info("[transcode %s -> h264] %s", codec or "?", path.relative_to(base_dir))
            if _transcode(ffmpeg_bin, path):
                converted += 1
            else:
                failed += 1

    logger.info(
        "完成：总数=%d 已是h264=%d 已转码=%d 失败=%d 缺失task=%d",
        total, skipped_h264, converted, failed, len(missing_tasks),
    )
    if missing_tasks:
        logger.info("缺失 task_ids: %s", missing_tasks)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
