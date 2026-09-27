"""
task/step 目录域 —— **把 step 目录当整体看**的那两件事：有哪些 task、有哪些 step。

    from app.storage import tasks as step_tasks
    for task_id in step_tasks.list_task_ids(order="recent"):
        for step_id in step_tasks.list_step_ids(task_id):
            ...

落盘结构（一次 run 一个目录，产物在 run 下按域隔离）：

    {root}/{task_id}/{step_id}/{run_id}/
      hls/        段 / init / playlist / sidecar / metadata
      inference/  detections.jsonl / temporal.jsonl / label_probs.npz

    list_task_ids(order=)     存储根下的 task id，按 id 升序 / 按最近一次 run 的开始时刻降序
    list_step_ids(task_id)    该 task 下的 step id，升序

**本模块不出定位能力**：往某个域里写东西是那个域自己的事，各域文件用
`_root.domain_dir(run, <自己的域>)` 取路径；run 的分配与查询归 `runs.py`。

依赖上界：stdlib only。规范见 `docs/kb/DESIGN_STORAGE_LAYER.md` §1。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Iterator, List, Tuple

from . import _root

logger = logging.getLogger(__name__)

__all__ = ["list_task_ids", "list_step_ids"]

# list_task_ids() 支持的排序。非法值炸而不是静默按默认走。
_VALID_ORDERS: Tuple[str, ...] = ("id", "recent")


def _iterdir(directory: Path) -> Iterator[Path]:
    """遍历目录，目录不存在 / 不可读时产出空序列（扫描期间消失的目录等于没扫到）。"""
    try:
        yield from directory.iterdir()
    except OSError as e:
        logger.debug("[Storage] 目录遍历失败，按空处理 %s: %s", directory, e)


def _step_id_dirs(task_id: int) -> Iterator[Tuple[int, Path]]:
    """该 task 下形如 `{step_id}/` 的子目录，产出 (step_id, 路径)。顺序同 iterdir。"""
    for entry in _iterdir(_root.path(task_id)):
        if not entry.is_dir():
            continue
        step_id = _root.dir_name_to_int(entry.name)
        if step_id is not None:
            yield step_id, entry


def list_step_ids(task_id: int) -> List[int]:
    """该 task 下的 step 子目录 id，升序。task 目录不存在返回 `[]`。

    只认数字目录名，文件与非 id 目录一律跳过。**不判断目录里有没有产物**——要按产物过滤
    的调用方自己去问对应的域（`hls.list_segments` 等）。
    """
    step_ids = [step_id for step_id, _ in _step_id_dirs(task_id)]
    step_ids.sort()
    return step_ids


def list_task_ids(order: str = "id") -> List[int]:
    """存储根下的 task 子目录 id。存储根不存在返回 `[]`。

    Args:
        order: `"id"` 升序；`"recent"` 按该 task 各 step 下最大的 `run_id` 降序（= 最近一次 run
            的开始时刻），同值时 task_id 大者优先。没有 run 目录的 task 排序键取 0（排最后）但
            仍在结果里，由调用方深扫时丢弃。

    Raises:
        ValueError: order 不在 ("id", "recent") 内。

    只认数字目录名，非 id 目录跳过、不报错。成本 O(目录数)：只列目录，不读文件。
    """
    if order not in _VALID_ORDERS:
        raise ValueError(f"Invalid order: {order!r}, expected one of {_VALID_ORDERS}")

    task_ids: List[int] = []
    for entry in _iterdir(_root.path()):
        if not entry.is_dir():
            continue
        task_id = _root.dir_name_to_int(entry.name)
        if task_id is not None:
            task_ids.append(task_id)

    if order == "id":
        return sorted(task_ids)

    keyed = [(_latest_run_id(task_id), task_id) for task_id in task_ids]
    keyed.sort(reverse=True)
    return [task_id for _, task_id in keyed]


def _latest_run_id(task_id: int) -> int:
    """该 task 各 step 下最大的 run_id；没有 run 目录返回 0。"""
    latest = 0
    for step_id, _ in _step_id_dirs(task_id):
        ids = _root.run_ids(task_id, step_id)
        if ids:
            latest = max(latest, ids[-1])
    return latest
