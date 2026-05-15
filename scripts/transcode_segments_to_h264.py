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
from typing import Dict, List, Optional, Tuple

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


def _resolve_ffprobe(ffmpeg_bin: str) -> str:
    """从 ffmpeg 路径推断匹配的 ffprobe 路径，找不到则回退到 PATH 里的 ffprobe。"""
    p = Path(ffmpeg_bin)
    if p.is_file():
        candidate = p.with_name(p.name.replace("ffmpeg", "ffprobe"))
        if candidate.exists():
            return str(candidate)
    return "ffprobe"


def _probe_duration(
    ffprobe_bin: str,
    path: Path,
    init_path: Optional[Path] = None,
) -> Tuple[Optional[float], float, bool]:
    """用 ffprobe 读取媒体时长。返回 (content_duration, input_start_time, is_already_fmp4)。

    - content_duration: 该段的真实媒体时长（秒），用于写 EXTINF + 累计 ts_offset
    - input_start_time: 输入文件的首 PTS（秒）。对已迁移的 fMP4 fragment 等于旧的 tfdt
      （来自上次 transcode 的 -output_ts_offset 残留）；对裸 mp4v / 普通 H.264 MP4 ≈ 0。
      transcode 时必须用 `effective_offset = desired_offset - input_start_time` 才能把
      输出 tfdt 钉到 desired_offset —— 否则 ffmpeg 的 -output_ts_offset 会**累加**到
      input PTS 之上，把已有的脏 tfdt 滚进新输出，hls.js 看到的位置就和 playlist 对不上。
    - is_already_fmp4: True 表示直接探测失败、拼 init+fragment 后成功 —— transcode 时
      要走拼接输入 + `-c:v copy`（无损 remux）

    - 直接探测成功 → 返回 (dur, start_time, False)
    - 拼接成功 → 返回 (dur, start_time, True)
    - 都失败 → 返回 (None, 0.0, False)
    """
    def _run(target: Path) -> Tuple[int, Optional[float], float, str]:
        try:
            result = subprocess.run(
                [
                    ffprobe_bin, "-v", "error",
                    "-show_entries", "format=duration,start_time",
                    "-of", "default=noprint_wrappers=1",
                    str(target),
                ],
                capture_output=True, text=True, timeout=30,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired) as e:
            return (-1, None, 0.0, f"{type(e).__name__}: {e}")
        if result.returncode != 0:
            return (result.returncode, None, 0.0, result.stderr.strip())

        # 解析 key=value 形式输出
        raw_duration: Optional[float] = None
        start_time = 0.0
        for line in result.stdout.splitlines():
            if "=" not in line:
                continue
            key, _, val = line.partition("=")
            val = val.strip()
            if val in ("N/A", ""):
                continue
            try:
                if key.strip() == "duration":
                    raw_duration = float(val)
                elif key.strip() == "start_time":
                    start_time = float(val)
            except ValueError:
                continue

        if raw_duration is None:
            return (0, None, 0.0, f"missing duration: {result.stdout!r}")

        # init+fragment 拼接后，ffprobe 把 format.duration 当作 max-PTS（含 tfdt 偏移），
        # 不是 fragment 自身的内容时长。减掉 start_time（= 首样本 PTS = tfdt）才是真实
        # 媒体时长。对普通无偏移 MP4，start_time≈0，运算等价于原 duration。
        content_duration = raw_duration - start_time

        # Sanity：极端情况下 ffprobe 可能直接报内容时长（不算偏移），此时 content_duration
        # 会变成大负数；回退到 raw_duration。0..3600s 视为合理。
        if 0 < content_duration <= 3600:
            return (0, content_duration, start_time, "")
        if 0 < raw_duration <= 3600:
            # 这种回退路径下 raw_duration 已是内容时长，input_start_time 视为 0
            return (0, raw_duration, 0.0, "")
        return (0, None, 0.0, f"implausible: raw={raw_duration} start={start_time}")

    rc, dur, start_time, err = _run(path)
    if dur is not None:
        return (dur, start_time, False)

    # 回退：fMP4 fragment 必须拼上 init 才能解析
    if init_path and init_path.exists():
        tmp = path.with_name(f".{path.stem}.probe.tmp.mp4")
        try:
            with tmp.open("wb") as out:
                out.write(init_path.read_bytes())
                out.write(path.read_bytes())
            _rc2, dur2, start_time2, err2 = _run(tmp)
            if dur2 is not None:
                return (dur2, start_time2, True)
            logger.warning(
                "ffprobe via concat failed: %s\n  direct stderr: %s\n  concat stderr: %s",
                path, err, err2,
            )
        except OSError as e:
            logger.warning("probe concat IO failed %s: %s", path, e)
        finally:
            tmp.unlink(missing_ok=True)
    else:
        logger.warning("ffprobe rc=%s: %s\nstderr: %s", rc, path, err)
    return (None, 0.0, False)


_SEGMENT_FNAME_RE = re.compile(r"^(raw|processed)_segment_(\d+)\.mp4$")


def _transcode_segment_to_fmp4(
    ffmpeg_bin: str,
    segment_path: Path,
    init_path: Path,
    capture_init: bool,
    ts_offset: float,
    is_already_fmp4: bool = False,
    input_start_time: float = 0.0,
) -> bool:
    """就地把一个段升级为 fMP4，必要时同时产出 init.mp4。

    - capture_init=True 且 init_path 不存在时，把 ffmpeg 产出的 init 移到 init_path
    - 其它情况下产出的 init 一概丢弃
    - ts_offset 由调用方按累计 EXTINF 算好（与 hls_strategy._ts_offset_seconds 同语义），
      让各 fragment 的 tfdt 累计 = playlist 时间线 = fragment 媒体时长
    - input_start_time 是输入文件的首 PTS（来自 _probe_duration）。`-output_ts_offset` 是
      "加在输入 PTS 之上"语义，要让输出 tfdt 等于 ts_offset 必须用
      `effective_offset = ts_offset - input_start_time`。否则旧的 tfdt 残留会被滚进新输出。
    - is_already_fmp4=True 时（--force 重跑已迁移目录）：
      * 输入改为 init+fragment 拼接后的临时文件（fMP4 fragment 单文件无 moov 无法被
        ffmpeg 直接读取）
      * codec 改为 `-c:v copy` —— 已经是 H.264，仅 remux 应用新 tfdt 即可，无须再编码

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
    tmp_input: Optional[Path] = None  # 拼接 init+fragment 的临时输入文件

    def _cleanup() -> None:
        for p in (tmp_init, tmp_segment, tmp_playlist):
            p.unlink(missing_ok=True)
        if tmp_input is not None:
            tmp_input.unlink(missing_ok=True)

    _cleanup()

    # 确定 ffmpeg 输入路径
    input_path = segment_path
    if is_already_fmp4:
        if not init_path.exists():
            logger.warning(
                "is_already_fmp4 but init.mp4 missing, cannot transcode: %s",
                segment_path,
            )
            return False
        tmp_input = target_dir / f".{stem}.tmp_input.mp4"
        try:
            with tmp_input.open("wb") as out:
                out.write(init_path.read_bytes())
                out.write(segment_path.read_bytes())
        except OSError as e:
            logger.warning("concat input failed %s: %s", segment_path, e)
            _cleanup()
            return False
        input_path = tmp_input

    cmd = [
        ffmpeg_bin,
        "-y",
        "-loglevel", "error",
        "-i", str(input_path),
    ]
    if is_already_fmp4:
        # 已是 H.264，仅 remux 应用新 tfdt offset，无质量损失
        cmd += ["-c:v", "copy"]
    else:
        cmd += [
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
        ]
    # ffmpeg -output_ts_offset 是 "加" 不是 "钉" 语义：
    #   output_pts = input_pts + offset
    # 我们希望 output_tfdt(= first output_pts) = ts_offset。input 首 PTS = input_start_time，
    # 所以 offset 必须等于 ts_offset - input_start_time。对新鲜 mp4v 输入此式 ≈ ts_offset。
    effective_offset = ts_offset - input_start_time
    cmd += [
        "-an",
        "-output_ts_offset", f"{effective_offset:.6f}",
        "-hls_segment_type", "fmp4",
        # 必须用绝对路径：ffmpeg 8.x 的 HLS muxer 把此处的 basename 解析到进程 cwd
        # 而不是 playlist 输出目录。详见 hls_strategy.py 同名注释。
        "-hls_fmp4_init_filename", str(tmp_init),
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
        if tmp_input is not None:
            tmp_input.unlink(missing_ok=True)
        return False

    tmp_playlist.unlink(missing_ok=True)
    if tmp_input is not None:
        tmp_input.unlink(missing_ok=True)
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


def _rewrite_playlist_header(
    path: Path,
    durations: Optional[Dict[str, float]] = None,
) -> bool:
    """把旧头部重写为 VERSION:7 + EXT-X-MAP，并以 durations 覆盖 EXTINF。

    - durations: 文件名 → 探测出的实际媒体时长（秒）。命中则覆盖原 EXTINF；
      未命中则保留原 EXTINF（兼容部分段 ffprobe 失败的场景）
    - EXTINF 必须与 fragment 媒体时长一致，否则 hls.js 段尾会卡 / 总时长会缩水
    - TARGETDURATION = ceil(max(EXTINF))，与 hls_strategy 写新段时一致
    """
    entries = _parse_playlist_entries(path)
    if not entries:
        logger.warning("playlist 空或解析失败，跳过重写: %s", path)
        return False

    durations = durations or {}
    new_entries: List[Tuple[float, str]] = []
    for old_dur, fname in entries:
        new_entries.append((durations.get(fname, old_dur), fname))

    target_duration = max(
        int(round(max((d for d, _ in new_entries), default=10.0))),
        1,
    )

    tmp_path = path.with_suffix(path.suffix + ".rewrite.tmp")
    lines: List[str] = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        '#EXT-X-MAP:URI="init.mp4"',
    ]
    for dur, fname in new_entries:
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


def _track_of(seg: Path) -> Optional[str]:
    m = _SEGMENT_FNAME_RE.match(seg.name)
    return m.group(1) if m else None


def _migrate_step(
    step_dir: Path,
    ffmpeg_bin: str,
    ffprobe_bin: str,
    base_dir: Path,
    dry_run: bool,
    force: bool,
) -> Tuple[int, int, int]:
    """迁移一个 step 目录。返回 (converted, failed, skipped_segments)。

    新策略（与 hls_strategy 写新段路径一致）：
    - 按 track 分组，按 ts_us 排序逐段转码
    - 转码前先 ffprobe 探测输入媒体时长 = 该段未来的 EXTINF
    - ts_offset 取该 track 已转好的累计 duration（即累计 EXTINF）
    - 转码完成后把探测出的 duration 累加到 cumulative
    - 最后用 durations 表覆盖 playlist 的 EXTINF —— 三套时间线对齐
    """
    init_path = step_dir / "init.mp4"
    raw_playlist = step_dir / "raw_playlist.m3u8"
    proc_playlist = step_dir / "processed_playlist.m3u8"

    already_migrated = (
        init_path.exists()
        and (not raw_playlist.exists() or _playlist_already_fmp4(raw_playlist))
        and (not proc_playlist.exists() or _playlist_already_fmp4(proc_playlist))
    )
    # 即使 fMP4 头部已存在，--force 仍要重转 + 重算 EXTINF —— 老脚本可能用 wall-clock
    # 公式生成过 EXTINF，需要刷新到 fragment 媒体时长。
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
            if pl.exists():
                logger.info("[dry-run] would rewrite header+EXTINF %s", pl.relative_to(base_dir))
        return (0, 0, 0)

    # 按 track 分组（_collect_segments 已经 sorted，但 raw / processed 是穿插的）
    raw_segs = [s for s in segments if _track_of(s) == "raw"]
    proc_segs = [s for s in segments if _track_of(s) == "processed"]

    # init 没产出过就让第一个段去捕获
    init_already = init_path.exists()
    converted = 0
    failed = 0
    durations_by_track: Dict[str, Dict[str, float]] = {"raw": {}, "processed": {}}

    for track_name, track_segs in (("raw", raw_segs), ("processed", proc_segs)):
        cumulative = 0.0
        for seg in track_segs:
            # 传入 init_path 让 --force 重跑场景能拼接探测 fMP4 fragment
            dur, input_start_time, is_already_fmp4 = _probe_duration(
                ffprobe_bin, seg, init_path,
            )
            if dur is None or dur <= 0:
                logger.warning(
                    "[probe failed] %s — 跳过该段（不更新 cumulative，不写 duration）",
                    seg.relative_to(base_dir),
                )
                failed += 1
                continue
            capture_init = not init_already
            ok = _transcode_segment_to_fmp4(
                ffmpeg_bin, seg, init_path, capture_init, cumulative,
                is_already_fmp4=is_already_fmp4,
                input_start_time=input_start_time,
            )
            if ok:
                converted += 1
                durations_by_track[track_name][seg.name] = dur
                cumulative += dur
                if capture_init and init_path.exists():
                    init_already = True
            else:
                failed += 1

    # 重写 playlist：无条件覆盖 EXTINF（新公式 = 探测出的 fragment 媒体时长）
    for pl, durations in (
        (raw_playlist, durations_by_track["raw"]),
        (proc_playlist, durations_by_track["processed"]),
    ):
        if pl.exists():
            ok = _rewrite_playlist_header(pl, durations=durations)
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
    parser.add_argument("--ffprobe", default=None, help="ffprobe 二进制路径（默认从 ffmpeg 路径推断）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s"
    )

    base_dir = _resolve_base_dir(args.base_dir)
    ffmpeg_bin = args.ffmpeg or _resolve_ffmpeg()
    ffprobe_bin = args.ffprobe or _resolve_ffprobe(ffmpeg_bin)

    if not base_dir.is_dir():
        logger.error("base_dir 不存在或不是目录: %s", base_dir)
        return 2

    logger.info("base_dir = %s", base_dir)
    logger.info("ffmpeg = %s", ffmpeg_bin)
    logger.info("ffprobe = %s", ffprobe_bin)
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
                sd, ffmpeg_bin, ffprobe_bin, base_dir, args.dry_run, args.force,
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
