"""
视频段拼接 & 上传脚本
用法：
    # 仅拼接（不上传）
    python concat_and_upload.py --db-dir ./database --output-dir ./merged_videos

    # 拼接 + 上传到标注服务器
    python concat_and_upload.py --db-dir ./database --output-dir ./merged_videos \
        --upload --remote-host 10.176.122.22 --remote-user root \
        --remote-dir /data/annotation/videos

    # 只拼接指定任务
    python concat_and_upload.py --db-dir ./database --output-dir ./merged_videos \
        --task-ids 12 15 20

依赖：ffmpeg 需在 PATH 中
"""

import argparse
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger(__name__)


def parse_playlist(playlist_path: Path) -> list[str]:
    """从 m3u8 播放列表解析有序的段文件名列表"""
    segments = []
    if not playlist_path.exists():
        return segments
    with playlist_path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#"):
                segments.append(line)
    return segments


def concat_task(task_dir: Path, output_dir: Path, seg_type: str = "raw") -> Path | None:
    """
    拼接单个任务目录下的所有视频段。

    Args:
        task_dir:   database/{client_id}/{task_id}/
        output_dir: 输出目录
        seg_type:   "raw" 或 "processed"

    Returns:
        输出文件路径，失败返回 None
    """
    playlist_path = task_dir / f"{seg_type}_playlist.m3u8"
    segments = parse_playlist(playlist_path)

    if not segments:
        # playlist 不存在时按文件名时间戳排序兜底
        segments = sorted(
            p.name for p in task_dir.glob(f"{seg_type}_segment_*.mp4")
        )

    if not segments:
        log.warning("  [跳过] 无 %s 段: %s", seg_type, task_dir)
        return None

    # 验证文件存在
    missing = [s for s in segments if not (task_dir / s).exists()]
    if missing:
        log.warning("  [警告] %d 个段文件缺失，将跳过: %s", len(missing), missing[:5])
        segments = [s for s in segments if (task_dir / s).exists()]

    log.info("  共 %d 个 %s 段，开始拼接...", len(segments), seg_type)

    # 读取 metadata 生成输出文件名
    metadata_path = task_dir / "metadata.json"
    task_id = task_dir.name
    client_id = task_dir.parent.name
    if metadata_path.exists():
        with metadata_path.open(encoding="utf-8") as f:
            meta = json.load(f)
        task_id = meta.get("task_id", task_id)
        client_id = meta.get("client_id", client_id)

    output_name = f"{client_id}__task{task_id}__{seg_type}.mp4"
    output_path = output_dir / output_name

    if output_path.exists():
        log.info("  [已存在，跳过] %s", output_path.name)
        return output_path

    # 写 ffmpeg concat list（用绝对路径，兼容含空格路径）
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".txt", delete=False, encoding="utf-8"
    ) as tmp:
        for seg in segments:
            abs_path = (task_dir / seg).resolve()
            # ffmpeg concat list 语法：路径中的单引号需转义
            escaped = str(abs_path).replace("'", "'\\''")
            tmp.write(f"file '{escaped}'\n")
        concat_list = tmp.name

    cmd = [
        "ffmpeg",
        "-y",                  # 覆盖已有文件
        "-f", "concat",
        "-safe", "0",
        "-i", concat_list,
        "-c", "copy",          # 无损拼接，不重新编码
        str(output_path),
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=600,
        )
        if result.returncode != 0:
            log.error("  ffmpeg 失败:\n%s", result.stderr[-2000:])
            return None
        size_mb = output_path.stat().st_size / 1024 / 1024
        log.info("  -> %s (%.1f MB)", output_path.name, size_mb)
        return output_path
    except subprocess.TimeoutExpired:
        log.error("  ffmpeg 超时（>10min）: %s", task_dir)
        return None
    except FileNotFoundError:
        log.error("ffmpeg 未找到，请确认已安装并在 PATH 中")
        sys.exit(1)
    finally:
        Path(concat_list).unlink(missing_ok=True)


def upload_file(local_path: Path, remote_host: str, remote_user: str, remote_dir: str) -> bool:
    """scp 上传单个文件"""
    remote_target = f"{remote_user}@{remote_host}:{remote_dir}/{local_path.name}"
    cmd = ["scp", "-o", "StrictHostKeyChecking=no", str(local_path), remote_target]
    log.info("上传: %s -> %s", local_path.name, remote_target)
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if result.returncode != 0:
        log.error("scp 失败: %s", result.stderr)
        return False
    log.info("上传成功: %s", local_path.name)
    return True


def main():
    parser = argparse.ArgumentParser(description="HLS 视频段拼接 & 上传工具")
    parser.add_argument("--db-dir", required=True, help="database/ 根目录路径")
    parser.add_argument("--output-dir", required=True, help="拼接结果输出目录")
    parser.add_argument(
        "--seg-type",
        choices=["raw", "processed", "both"],
        default="raw",
        help="拼接类型（默认 raw）",
    )
    parser.add_argument(
        "--task-ids",
        nargs="+",
        type=int,
        help="只处理指定 task_id（不指定则处理全部）",
    )
    parser.add_argument("--upload", action="store_true", help="拼接后上传到远端")
    parser.add_argument("--remote-host", default="10.176.122.22", help="标注服务器 IP")
    parser.add_argument("--remote-user", default="root", help="SSH 用户名")
    parser.add_argument("--remote-dir", default="/data/annotation/videos", help="远端目标目录")
    args = parser.parse_args()

    db_dir = Path(args.db_dir).resolve()
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    if not db_dir.exists():
        log.error("database 目录不存在: %s", db_dir)
        sys.exit(1)

    # 发现所有 task 目录：database/{client_id}/{task_id}/
    task_dirs: list[Path] = []
    for client_dir in sorted(db_dir.iterdir()):
        if not client_dir.is_dir():
            continue
        for task_dir in sorted(client_dir.iterdir()):
            if not task_dir.is_dir():
                continue
            if args.task_ids and not any(
                task_dir.name == str(tid) for tid in args.task_ids
            ):
                continue
            task_dirs.append(task_dir)

    if not task_dirs:
        log.warning("未找到任何任务目录")
        sys.exit(0)

    log.info("发现 %d 个任务目录", len(task_dirs))

    seg_types = ["raw", "processed"] if args.seg_type == "both" else [args.seg_type]

    merged_files: list[Path] = []
    failed: list[str] = []

    for task_dir in task_dirs:
        log.info("处理任务: %s/%s", task_dir.parent.name, task_dir.name)
        for seg_type in seg_types:
            out = concat_task(task_dir, output_dir, seg_type)
            if out:
                merged_files.append(out)
            else:
                failed.append(f"{task_dir.parent.name}/{task_dir.name} [{seg_type}]")

    log.info("=== 拼接完成: 成功 %d，失败 %d ===", len(merged_files), len(failed))
    if failed:
        log.warning("失败列表:\n  %s", "\n  ".join(failed))

    if args.upload and merged_files:
        log.info("开始上传到 %s:%s ...", args.remote_host, args.remote_dir)
        upload_ok = 0
        for f in merged_files:
            if upload_file(f, args.remote_host, args.remote_user, args.remote_dir):
                upload_ok += 1
        log.info("=== 上传完成: %d/%d ===", upload_ok, len(merged_files))


if __name__ == "__main__":
    main()