"""盘上原语 —— 整体替换、原子删除、建一级目录，全包只此一份。**包内私有**（唯一的包外
调用方是 `cleanup_worker`，只用 `remove` / `purge_trash`）。

    _fs.replace(path, lambda tmp: tmp.write_text(s))   # 同目录 tmp + os.replace
    _fs.remove(step_dir)                               # rename 进 {root}/.trash/ 再 rmtree
    _fs.ensure_dir(run_dir / "hls")                    # 只建这一级

三条硬约束：

- **`replace` / `ensure_dir` 不建父目录**：父目录不在就 `OSError`。产物目录只由分配者建，
  写者建父目录会在回收后重建出僵尸目录。
- **`remove` 的 `root` 必须与 `path` 同卷**：回收区就是 `{root}/.trash/`，同卷 rename 才原子。
- **`.trash/` 名字非数字**，task / step 枚举天然跳过它。

依赖上界：stdlib only。设计见 `docs/update/20260927_STORAGE_RUN_DIR_PROPOSAL.md` §1。
"""

from __future__ import annotations

import enum
import logging
import os
import shutil
import uuid
from pathlib import Path
from typing import Callable, Optional

from . import _root

logger = logging.getLogger(__name__)

__all__ = ["Removed", "TRASH_NAME", "ensure_dir", "purge_trash", "remove", "replace"]

# 回收区目录名：在存储根下，与所有产物同卷。
TRASH_NAME = ".trash"


class Removed(enum.Enum):
    """`remove` 的三态结果。"""

    ABSENT = "absent"    # 本来就不在
    REMOVED = "removed"  # 已从原位置消失（rmtree 失败的残留在回收区，下一轮再清）
    FAILED = "failed"    # rename 失败，盘上原样不动


def replace(path: Path, write_fn: Callable[[Path], None]) -> None:
    """整体替换 `path`：`write_fn` 写同目录的 `.{name}.tmp`，再 `os.replace` 换名。

    Raises:
        OSError: 写 tmp / 换名失败（含父目录不存在）。失败时删掉 tmp，原文件原样保留。
        以及 `write_fn` 自己抛出的任何异常，同样先删 tmp 再原样上抛。
    """
    tmp = path.with_name("." + path.name + ".tmp")
    try:
        write_fn(tmp)
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:  # 清 tmp 再失败不能盖掉原始错因
            pass
        raise


def remove(path: Path, *, root: Optional[Path] = None) -> Removed:
    """原子删除 `path`（文件或目录）：先 rename 到 `{root}/.trash/{uuid}`，再 `rmtree`。

    Args:
        root: 回收区所在的根，缺省为存储根。必须与 `path` 同卷。

    Returns:
        `Removed` 三态；失败只记 warning，不抛。
    """
    if not os.path.lexists(path):
        return Removed.ABSENT
    trash = (root if root is not None else _root.path()) / TRASH_NAME
    target = trash / uuid.uuid4().hex
    try:
        trash.mkdir(exist_ok=True)
        os.rename(path, target)
    except FileNotFoundError:
        if not os.path.lexists(path):  # 并发下被别人先删了
            return Removed.ABSENT
        logger.warning("[storage.fs] 移入回收区失败（回收区不可建）%s", path, exc_info=True)
        return Removed.FAILED
    except OSError as e:
        logger.warning("[storage.fs] 移入回收区失败，盘上原样不动 %s: %s", path, e)
        return Removed.FAILED
    _rmtree(target)
    return Removed.REMOVED


def purge_trash(*, root: Optional[Path] = None) -> int:
    """清空 `{root}/.trash/`，返回清掉的条目数。删不掉的留着，下一轮再清；回收区不在返回 0。"""
    trash = (root if root is not None else _root.path()) / TRASH_NAME
    try:
        entries = list(trash.iterdir())
    except OSError:
        return 0
    return sum(1 for entry in entries if _rmtree(entry))


def ensure_dir(path: Path) -> Path:
    """确保 `path` 这一级目录存在（`mkdir(exist_ok=True)`，**不带 parents**），返回 `path`。

    Raises:
        OSError: 父目录不存在（`FileNotFoundError`）或建目录失败。
    """
    path.mkdir(exist_ok=True)
    return path


def _rmtree(path: Path) -> bool:
    try:
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        return True
    except OSError as e:
        logger.warning("[storage.fs] 回收区条目删除失败，留待下一轮 %s: %s", path, e)
        return False
