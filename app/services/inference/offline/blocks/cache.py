"""离线中间产物缓存与回收。

    {offline_base_dir}/.cache/{task}/{step}/<name>.npz

**这棵树下的东西全部可重建**——视觉块、导出样例、逐帧推理结果都能靠重跑拿回来。
正式结果是 storage 里的 `facts.jsonl`（SegmentFact），不落这里。因此 gc 不需要引用
计数、不需要内容寻址、也没有需要人判断的例外：过期即删。

两条硬约束（违反会被在线的 StorageCleanupWorker 误伤）：
    - 产物不落 `{storage_base_dir}/{task}/{step}/`——那里受 cleanup_days（默认 7 天）TTL；
    - 任何 manifest 不叫 `metadata.json`——Worker 按 `{base}/*/*/metadata.json` 判定
      过期 step 目录并 rmtree 整个目录。
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import numpy as np

from app.services.inference.offline.models import FeatureBlock
from app.settings import settings

logger = logging.getLogger(__name__)

_CACHE_SUBDIR = ".cache"

# 默认保留 30 天。**刻意长于 raw 段的 cleanup_days（7 天）**：raw 一过期，视觉块就
# 永久不可重建，那时它反而更宝贵，不该跟着一起蒸发。
DEFAULT_TTL_DAYS = 30


def cache_root(base_dir: Optional[Path] = None) -> Path:
    base = Path(base_dir) if base_dir is not None else settings.offline_base_dir
    return Path(base) / _CACHE_SUBDIR


def cache_dir(task_id: int, step_id: int, base_dir: Optional[Path] = None) -> Path:
    return cache_root(base_dir) / str(task_id) / str(step_id)


def write_block(
    path: Path, block: FeatureBlock, extra: Optional[Dict[str, Any]] = None
) -> None:
    """落一块到 npz（覆盖写：同 key 重跑即覆盖，无需先清理旧产物）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(path, **block.to_npz_payload(extra))


def read_block(path: Path) -> Optional[Tuple[FeatureBlock, Dict[str, Any]]]:
    """读一块；文件不存在或损坏返回 None（缓存失效不该让调用方崩，重算即可）。"""
    if not path.exists():
        return None
    try:
        with np.load(path, allow_pickle=False) as data:
            return FeatureBlock.from_npz(data)
    except Exception as e:
        logger.warning("[offline.cache] 缓存损坏，按未命中处理 %s: %s", path, e)
        return None


def sweep(
    ttl_days: int = DEFAULT_TTL_DAYS,
    base_dir: Optional[Path] = None,
    storage_dir: Optional[Path] = None,
) -> int:
    """清理过期缓存 + 残留临时 m3u8，返回释放字节数。

    每次离线执行开头调一次——离线是手动跑的实验工具，值守节奏用不着单开 gc 命令，
    也不该进在线 lifespan。
    """
    freed = _sweep_cache(ttl_days, base_dir)
    freed += _sweep_stray_playlists(storage_dir)
    if freed:
        logger.info("[offline.cache] gc 释放 %.1f MB", freed / 1024 / 1024)
    return freed


def _sweep_cache(ttl_days: int, base_dir: Optional[Path]) -> int:
    root = cache_root(base_dir)
    if not root.exists():
        return 0
    deadline = time.time() - max(0, int(ttl_days)) * 86400
    freed = 0
    for path in root.rglob("*.npz"):
        try:
            stat = path.stat()
            if stat.st_mtime >= deadline:
                continue
            size = stat.st_size
            path.unlink()
            freed += size
            logger.info("[offline.cache] 过期删除 %s (%.1f MB)", path, size / 1024 / 1024)
        except OSError as e:
            logger.warning("[offline.cache] 删除失败 %s: %s", path, e)
    _prune_empty_dirs(root)
    return freed


def _sweep_stray_playlists(storage_dir: Optional[Path]) -> int:
    """清 FrameSource 留下的 `.export_*.m3u8`。

    它在 `finally` 里 unlink，但进程被 kill 时不执行——没人收就会一直躺在 step 目录里。
    """
    base = Path(storage_dir) if storage_dir is not None else settings.storage_base_dir
    if not base.exists():
        return 0
    freed = 0
    for path in base.glob("*/*/.export_*.m3u8"):
        try:
            size = path.stat().st_size
            path.unlink()
            freed += size
            logger.info("[offline.cache] 清理残留临时播放列表 %s", path)
        except OSError:
            continue
    return freed


def _prune_empty_dirs(root: Path) -> None:
    """自底向上删空目录，避免缓存清完后留一地空壳。"""
    for path in sorted(root.rglob("*"), key=lambda p: len(p.parts), reverse=True):
        if path.is_dir() and not any(path.iterdir()):
            try:
                path.rmdir()
            except OSError:
                continue
