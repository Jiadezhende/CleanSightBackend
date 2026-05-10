"""
旧落盘目录迁移：{base_dir}/{client_id}/{task_id}/  →  {base_dir}/{task_id}/{step_id}/

背景：原版落盘按 source_ip 分区（client_id 即 clean_task.source_ip），但 step 切洗消台
时该字段会被业务侧覆写，导致 traceback 找不到旧 step 的文件。新版按 (task_id, step_id)
分区。

策略：
- 扫描 base_dir 下所有形如 IP/数字 task_id 的二级目录
- 优先从该目录的 metadata.json 读出 step_id
- 缺失或非法时使用 --default-step-id（默认 0）作为 sentinel
- 把整个目录原子移动到新位置 {base_dir}/{task_id}/{step_id}/
- 已存在新目录 → 默认跳过；--merge 模式按文件名归并、冲突保留旧文件并 WARNING

用法：
    python -m scripts.migrate_legacy_runs --dry-run              # 仅打印计划
    python -m scripts.migrate_legacy_runs                        # 执行迁移
    python -m scripts.migrate_legacy_runs --base-dir /path/db    # 显式 base_dir
    python -m scripts.migrate_legacy_runs --default-step-id 0    # 占位 step_id
    python -m scripts.migrate_legacy_runs --merge                # 目标存在时合并

注：
- 不可逆 —— 建议先 --dry-run + 备份 base_dir。
- 不修改文件内容，仅移动 / 重命名。
- 历史 metadata.json 里的 client_id 字段会保留（迁移脚本不重写文件），
  新代码不读该字段。
"""

from __future__ import annotations

import argparse
import json
import logging
import re
import shutil
import sys
from pathlib import Path
from typing import List, Optional, Tuple

logger = logging.getLogger("migrate_legacy_runs")


_TASK_ID_PATTERN = re.compile(r"^\d+$")
# 简单的 IPv4 / 主机名匹配（兼容老路径里出现的各种 source_ip）
_LEGACY_PARENT_PATTERN = re.compile(r"^[A-Za-z0-9._:-]+$")


def _read_step_id_from_metadata(task_dir: Path) -> Optional[int]:
    """尝试从 metadata.json 读 step_id；缺失或非法返回 None。"""
    md_path = task_dir / "metadata.json"
    if not md_path.exists():
        return None
    try:
        with md_path.open("r", encoding="utf-8") as f:
            md = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("metadata.json 解析失败 %s: %s", md_path, e)
        return None
    raw = md.get("step_id")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning("metadata.json step_id 非法 %s: %r", md_path, raw)
        return None


def _scan_legacy_dirs(base_dir: Path) -> List[Tuple[Path, int]]:
    """返回 [(legacy_dir, task_id)] 列表 —— 形如 base/<client_id>/<task_id>/。

    新版目录是 base/<task_id>/<step_id>/，第一层就是数字。本扫描跳过：
    - 第一层目录名是数字（已是新结构）
    - 第二层目录名不是数字
    - 隐藏目录（以 . 开头）
    """
    if not base_dir.is_dir():
        return []
    out: List[Tuple[Path, int]] = []
    for first in sorted(base_dir.iterdir()):
        if not first.is_dir():
            continue
        if first.name.startswith("."):
            continue
        if _TASK_ID_PATTERN.match(first.name):
            # 已是新结构：base/<task_id>/<step_id>/
            continue
        if not _LEGACY_PARENT_PATTERN.match(first.name):
            logger.debug("跳过非常规目录名 %s", first)
            continue
        for second in sorted(first.iterdir()):
            if not second.is_dir():
                continue
            if not _TASK_ID_PATTERN.match(second.name):
                continue
            out.append((second, int(second.name)))
    return out


def _merge_into(src: Path, dst: Path) -> Tuple[int, int]:
    """把 src 下的内容合并到 dst（已存在）。

    返回 (moved, conflicts)。冲突时保留 dst 现有文件，源文件留在 src。
    """
    moved = 0
    conflicts = 0
    for entry in src.rglob("*"):
        if not entry.is_file():
            continue
        rel = entry.relative_to(src)
        target = dst / rel
        if target.exists():
            logger.warning("冲突：目标已存在，保留 dst 文件：%s", target)
            conflicts += 1
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(entry), str(target))
        moved += 1
    # 清理空源目录
    try:
        for sub in sorted(src.rglob("*"), key=lambda p: -len(p.parts)):
            if sub.is_dir() and not any(sub.iterdir()):
                sub.rmdir()
        if src.exists() and not any(src.iterdir()):
            src.rmdir()
    except OSError as e:
        logger.warning("清理空目录失败 %s: %s", src, e)
    return moved, conflicts


def _migrate_one(
    legacy_dir: Path,
    task_id: int,
    base_dir: Path,
    default_step_id: int,
    dry_run: bool,
    merge: bool,
) -> str:
    """迁移单个旧目录。返回一行可读摘要。"""
    step_id = _read_step_id_from_metadata(legacy_dir)
    resolved_from = "metadata.json"
    if step_id is None:
        step_id = default_step_id
        resolved_from = f"default(--default-step-id={default_step_id})"

    new_dir = base_dir / str(task_id) / str(step_id)

    if new_dir.exists():
        if not merge:
            return (
                f"SKIP {legacy_dir} → {new_dir} (target exists; pass --merge to merge)"
            )
        if dry_run:
            n = sum(1 for _ in legacy_dir.rglob("*") if _.is_file())
            return f"WOULD MERGE {n} files: {legacy_dir} → {new_dir}"
        moved, conflicts = _merge_into(legacy_dir, new_dir)
        return f"MERGED {moved} files (conflicts={conflicts}): {legacy_dir} → {new_dir}"

    if dry_run:
        return (
            f"WOULD MOVE {legacy_dir} → {new_dir} "
            f"(step_id from {resolved_from})"
        )

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(legacy_dir), str(new_dir))
    # 清理可能空了的旧 client_id 目录
    parent = legacy_dir.parent
    try:
        if parent.exists() and not any(parent.iterdir()):
            parent.rmdir()
    except OSError:
        pass
    return f"MOVED {legacy_dir} → {new_dir} (step_id from {resolved_from})"


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--base-dir",
        type=Path,
        default=None,
        help="持久化根目录；默认读 PersistenceConfig.storage_base_dir",
    )
    parser.add_argument(
        "--default-step-id",
        type=int,
        default=0,
        help="metadata.json 缺失或非法时使用的占位 step_id（默认 0）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划不实际移动",
    )
    parser.add_argument(
        "--merge",
        action="store_true",
        help="目标 (task_id, step_id) 目录已存在时按文件名归并；冲突保留 dst",
    )
    parser.add_argument(
        "--verbose",
        "-v",
        action="store_true",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )

    if args.base_dir is None:
        from app.services.persistence.config import get_persistence_config
        base_dir = get_persistence_config().storage_base_dir
    else:
        base_dir = args.base_dir.resolve()

    if not base_dir.exists():
        logger.error("base_dir 不存在: %s", base_dir)
        return 2

    logger.info("扫描根目录: %s", base_dir)
    legacy = _scan_legacy_dirs(base_dir)
    logger.info("发现旧目录数量: %d", len(legacy))

    for path, task_id in legacy:
        line = _migrate_one(
            legacy_dir=path,
            task_id=task_id,
            base_dir=base_dir,
            default_step_id=args.default_step_id,
            dry_run=args.dry_run,
            merge=args.merge,
        )
        logger.info(line)

    if args.dry_run:
        logger.info("dry-run 完成 —— 未做任何修改")
    else:
        logger.info("迁移完成")
    return 0


if __name__ == "__main__":
    sys.exit(main())
