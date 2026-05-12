"""
批量升级历史 mp4 段为 HLS-ready fMP4 + 生成 step 级 init.mp4 + 重写 playlist 头

背景：
- 早期段是 cv2 mp4v（MPEG-4 Part 2），后来升级为 H.264 普通 MP4 + faststart
- 当前 hls.js 走 m3u8 播放时报 fragParsingError —— HLS 要求 MP4 段必须是 fragmented MP4
- 本脚本把任意历史 mp4（mp4v / h264 普通 MP4）就地升级为 fMP4，并按 step 写一份共享
  init.mp4，最后重写 m3u8 头（升 VERSION:7 + 插入 #EXT-X-MAP）

策略：
- 按 step 目录维度迁移（外层 task_id，内层 step_id）
- 同 step 内所有段共享一份 init.mp4：第一个段产出时落盘，后续段产出的 init 丢弃
- 幂等：step 目录已存在 init.mp4 且 playlist 已含 #EXT-X-MAP 则跳过（除非 --force）
- 单段失败仅 WARN 并继续，不中断批次

用法：
    python -m scripts.transcode_segments_to_h264 1778293239052
    python -m scripts.transcode_segments_to_h264 1778293239052 1778293240000
    python -m scripts.transcode_segments_to_h264 --base-dir /data/db 1778293239052
    python -m scripts.transcode_segments_to_h264 --dry-run 1778293239052
    python -m scripts.transcode_segments_to_h264 --force 1778293239052
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from typing import List, Optional, Tuple

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


_SEGMENT_FNAME_RE = re.compile(r"^(raw|processed)_segment_(\d+)\.mp4$")


def _ts_offset_seconds(segment_path: Path) -> float:
    """计算当前段相对该 step+track 首段的偏移（秒）。

    与 hls_strategy._ts_offset_seconds 同语义：每个段独立转码，输入 PTS 从 0 起，不补
    偏移则所有 fragment 的 tfdt 都是 0，hls.js VOD 连续播放会卡在段尾。
    """
    m = _SEGMENT_FNAME_RE.match(segment_path.name)
    if not m:
        return 0.0
    track, current_ts = m.group(1), int(m.group(2))
    min_ts = current_ts
    try:
        for entry in segment_path.parent.iterdir():
            em = _SEGMENT_FNAME_RE.match(entry.name)
            if em and em.group(1) == track:
                ts = int(em.group(2))
                if ts < min_ts:
                    min_ts = ts
    except OSError:
        return 0.0
    return max(0.0, (current_ts - min_ts) / 1_000_000.0)


def _transcode_segment_to_fmp4(
    ffmpeg_bin: str,
    segment_path: Path,
    init_path: Path,
    capture_init: bool,
) -> bool:
    """就地把一个段升级为 fMP4，必要时同时产出 init.mp4。

    - capture_init=True 且 init_path 不存在时，把 ffmpeg 产出的 init 移到 init_path
    - 其它情况下产出的 init 一概丢弃
    - 用 -output_ts_offset 把各段 PTS 起点累计起来，保证 hls.js 连续播放不停在段尾

    返回 True 表示段已替换为 fMP4。
    """
    target_dir = segment_path.parent
    stem = segment_path.stem
    tmp_init = target_dir / f".{stem}.tmp_init.mp4"
    # ffmpeg HLS muxer 强制 -hls_segment_filename 含 %d 模板（即便单段），
    # 否则报 "Invalid segment filename template"。pin -start_number 0 让产物固定为 _0.mp4
    tmp_segment_template = target_dir / f".{stem}.tmp_seg_%d.mp4"
    tmp_segment = target_dir / f".{stem}.tmp_seg_0.mp4"
    tmp_playlist = target_dir / f".{stem}.tmp.m3u8"
    ts_offset = _ts_offset_seconds(segment_path)

    def _cleanup() -> None:
        for p in (tmp_init, tmp_segment, tmp_playlist):
            p.unlink(missing_ok=True)

    _cleanup()

    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel", "error",
        "-i", str(segment_path),
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-an",
        "-output_ts_offset", f"{ts_offset:.6f}",
        "-hls_segment_type", "fmp4",
        "-hls_fmp4_init_filename", tmp_init.name,
        "-hls_segment_filename", str(tmp_segment_template),
        "-start_number", "0",
        "-hls_time", "99999",
        "-hls_list_size", "0",
        "-hls_flags", "temp_file",
        "-f", "hls",
        str(tmp_playlist),
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        logger.warning("ffmpeg failed (%s): %s", type(e).__name__, segment_path)
        _cleanup()
        return False

    if result.returncode != 0 or not tmp_segment.exists():
        logger.warning(
            "ffmpeg rc=%s: %s\nstderr: %s",
            result.returncode, segment_path, result.stderr.strip(),
        )
        _cleanup()
        return False

    # init.mp4 落盘：仅当 capture_init 且尚未存在
    if tmp_init.exists():
        if capture_init and not init_path.exists():
            try:
                os.replace(tmp_init, init_path)
            except OSError as e:
                logger.warning("install init.mp4 failed %s: %s", init_path, e)
                tmp_init.unlink(missing_ok=True)
        else:
            tmp_init.unlink(missing_ok=True)

    # 段原地替换
    try:
        os.replace(tmp_segment, segment_path)
    except OSError as e:
        logger.warning("replace segment failed %s: %s", segment_path, e)
        tmp_segment.unlink(missing_ok=True)
        return False

    tmp_playlist.unlink(missing_ok=True)
    return True


# ---------------------------------------------------------------------------
# Playlist 重写
# ---------------------------------------------------------------------------


_EXTINF_PATTERN = re.compile(r"^#EXTINF:([0-9.]+),?$")


def _parse_playlist_entries(path: Path) -> List[Tuple[float, str]]:
    """从旧 m3u8 解析出 [(duration, filename), ...]，按文件出现顺序。"""
    entries: List[Tuple[float, str]] = []
    if not path.exists():
        return entries

    current_dur: Optional[float] = None
    for line in path.read_text(encoding="utf-8").splitlines():
        s = line.strip()
        if not s:
            continue
        m = _EXTINF_PATTERN.match(s)
        if m:
            try:
                current_dur = float(m.group(1))
            except ValueError:
                current_dur = None
            continue
        if s.startswith("#"):
            continue
        # 资源行
        if current_dur is not None:
            entries.append((current_dur, s))
            current_dur = None
    return entries


def _playlist_already_fmp4(path: Path) -> bool:
    if not path.exists():
        return False
    head = path.read_text(encoding="utf-8")[:1024]
    return "#EXT-X-MAP:" in head and "#EXT-X-VERSION:7" in head


def _rewrite_playlist_header(path: Path, target_duration: int = 10) -> bool:
    """把旧 VERSION:3 头部重写为 VERSION:7 + EXT-X-MAP。保留所有 EXTINF + 段文件名。"""
    entries = _parse_playlist_entries(path)
    if not entries:
        logger.warning("playlist 空或解析失败，跳过重写: %s", path)
        return False

    tmp_path = path.with_suffix(path.suffix + ".rewrite.tmp")
    lines: List[str] = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for dur, fname in entries:
        lines.append(f"#EXTINF:{dur:.3f},")
        lines.append(fname)
    tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.replace(tmp_path, path)
    except OSError as e:
        logger.warning("rewrite playlist replace failed %s: %s", path, e)
        tmp_path.unlink(missing_ok=True)
        return False
    return True


# ---------------------------------------------------------------------------
# 目录遍历 & 主流程
# ---------------------------------------------------------------------------


def _is_step_dir(d: Path) -> bool:
    """step 目录的判定：名字是数字 + 含 *_segment_*.mp4 或 *_playlist.m3u8。"""
    if not d.is_dir():
        return False
    if not d.name.isdigit():
        return False
    has_segment = any(d.glob("*_segment_*.mp4"))
    has_playlist = any(d.glob("*_playlist.m3u8"))
    return has_segment or has_playlist


def _collect_segments(step_dir: Path) -> List[Path]:
    return sorted(
        p for p in step_dir.glob("*_segment_*.mp4")
        if not p.name.startswith(".") and not p.name.endswith(".transcode.tmp")
    )


def _migrate_step(
    step_dir: Path,
    ffmpeg_bin: str,
    base_dir: Path,
    dry_run: bool,
    force: bool,
) -> Tuple[int, int, int]:
    """迁移一个 step 目录。返回 (converted, failed, skipped_segments)。"""
    init_path = step_dir / "init.mp4"
    raw_playlist = step_dir / "raw_playlist.m3u8"
    proc_playlist = step_dir / "processed_playlist.m3u8"

    already_migrated = (
        init_path.exists()
        and (not raw_playlist.exists() or _playlist_already_fmp4(raw_playlist))
        and (not proc_playlist.exists() or _playlist_already_fmp4(proc_playlist))
    )
    if already_migrated and not force:
        logger.info("[skip migrated] %s", step_dir.relative_to(base_dir))
        segments = _collect_segments(step_dir)
        return (0, 0, len(segments))

    segments = _collect_segments(step_dir)
    if not segments:
        logger.info("[no segments] %s", step_dir.relative_to(base_dir))
        return (0, 0, 0)

    logger.info(
        "[migrating step] %s (%d segments)",
        step_dir.relative_to(base_dir), len(segments),
    )

    if dry_run:
        for seg in segments:
            logger.info("[dry-run] would transcode %s", seg.relative_to(base_dir))
        if not init_path.exists():
            logger.info("[dry-run] would create %s", init_path.relative_to(base_dir))
        for pl in (raw_playlist, proc_playlist):
            if pl.exists() and not _playlist_already_fmp4(pl):
                logger.info("[dry-run] would rewrite header %s", pl.relative_to(base_dir))
        return (0, 0, 0)

    # init 没产出过就让第一个段去捕获
    init_already = init_path.exists()
    converted = 0
    failed = 0
    for seg in segments:
        capture_init = not init_already
        if _transcode_segment_to_fmp4(ffmpeg_bin, seg, init_path, capture_init):
            converted += 1
            if capture_init and init_path.exists():
                init_already = True
        else:
            failed += 1

    # 重写 playlist 头部
    for pl in (raw_playlist, proc_playlist):
        if pl.exists() and not _playlist_already_fmp4(pl):
            ok = _rewrite_playlist_header(pl)
            logger.info(
                "[playlist %s] %s",
                "rewritten" if ok else "rewrite-failed",
                pl.relative_to(base_dir),
            )

    return (converted, failed, 0)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("task_ids", nargs="+", help="一个或多个 task_id（数字）")
    parser.add_argument("--base-dir", default=None, help="持久化 base_dir（默认读 persistence_config）")
    parser.add_argument("--dry-run", action="store_true", help="只打印计划，不实际转码")
    parser.add_argument("--force", action="store_true", help="忽略已迁移标志，强制重转")
    parser.add_argument("--ffmpeg", default=None, help="ffmpeg 二进制路径（默认读 settings.ffmpeg_path）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    base_dir = _resolve_base_dir(args.base_dir)
    ffmpeg_bin = args.ffmpeg or _resolve_ffmpeg()

    if not base_dir.is_dir():
        logger.error("base_dir 不存在或不是目录: %s", base_dir)
        return 2

    logger.info("base_dir = %s", base_dir)
    logger.info("ffmpeg = %s", ffmpeg_bin)
    logger.info("task_ids = %s", args.task_ids)
    if args.force:
        logger.info("--force 已开启：忽略已迁移标志")
    if args.dry_run:
        logger.info("--dry-run 已开启：只打印计划")

    total_converted = 0
    total_failed = 0
    total_skipped = 0
    missing_tasks: List[str] = []
    migrated_steps = 0
    skipped_steps = 0

    for tid in args.task_ids:
        task_dir = base_dir / tid
        if not task_dir.is_dir():
            logger.warning("task 目录不存在，跳过: %s", task_dir)
            missing_tasks.append(tid)
            continue

        step_dirs = sorted(d for d in task_dir.iterdir() if _is_step_dir(d))
        if not step_dirs:
            logger.info("[task %s] 无 step 子目录", tid)
            continue

        logger.info("[task %s] 发现 %d 个 step 目录", tid, len(step_dirs))
        for sd in step_dirs:
            converted, failed, skipped = _migrate_step(
                sd, ffmpeg_bin, base_dir, args.dry_run, args.force,
            )
            total_converted += converted
            total_failed += failed
            total_skipped += skipped
            if converted or failed:
                migrated_steps += 1
            elif skipped:
                skipped_steps += 1

    logger.info(
        "完成：转码段=%d 失败段=%d 跳过段=%d 已迁移step=%d 跳过step=%d 缺失task=%d",
        total_converted, total_failed, total_skipped,
        migrated_steps, skipped_steps, len(missing_tasks),
    )
    if missing_tasks:
        logger.info("缺失 task_ids: %s", missing_tasks)
    return 0 if total_failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
