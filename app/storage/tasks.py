"""
task/step 目录域 —— **把 step 目录当整体看**的那三件事：有哪些 task、有哪些 step、整个删掉。

    from app.storage import tasks as step_tasks
    for task_id in step_tasks.ids(order="mtime"):
        for step_id in step_tasks.steps(task_id):
            ...

落盘结构（产物按域隔离，step 根下只有域目录、没有文件）：

    {root}/{task_id}/{step_id}/
      hls/       段 / init / playlist / sidecar / metadata
      features/  features.jsonl / facts.jsonl
      lab/       送标与导出的临时件（用完即删，残留随 step TTL 回收）

**本模块不出定位能力**：往某个域里写东西是那个域自己的事，各域文件（`hls` / `feature` /
`lab`）用 `_root.path(task_id, step_id, <自己的域>)` 取路径。本模块只在「跨所有域」时出面
——枚举的是目录本身，删除的是整个 step。

**为什么这三件事在这里、不在 `_root`**：`steps()` / `ids()` 要 `iterdir` + `stat`，
`purge_step()` 要 `rmtree`，都碰盘且都与「有哪些/删哪个」有关；而 `_root` 只回答「在哪」。
且 `purge_step` 跨**所有**域（一次 `rmtree` 把 `hls/`、`features/`、`lab/` 全带走），它不
属于任何单一域，就是 task/step 目录这一层自己的能力。

依赖上界：stdlib only（`_root` 也是）。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Iterator, List, Tuple

from app.storage import _root

logger = logging.getLogger(__name__)

__all__ = ["steps", "ids", "purge_step"]

# ids() 支持的排序。非法值必须炸而不是静默按默认走——传错 order 说明调用方对返回顺序
# 有预期，给它一个不符预期的顺序比报错更坏。
_VALID_ORDERS: Tuple[str, ...] = ("id", "mtime")


def _iterdir(directory: Path) -> Iterator[Path]:
    """遍历目录，目录不存在/不可读时产出空序列。

    枚举与并发删除天然会撞车（`cleanup_worker` 的 TTL 回收、重启 supersede 都在删目录），
    撞上时该目录本就不该出现在结果里，静默跳过是对的——不是吞异常，是"扫描期间消失的
    目录等于没扫到"。
    """
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


def steps(task_id: int) -> List[int]:
    """该 task 下的 step 子目录 id，升序。task 目录不存在返回 `[]`。

    只认数字目录名（`_root.dir_name_to_int`），文件与非 id 目录一律跳过。

    **不判断目录里有没有产物**——「两轨都没段的 step 算不算数」是 HLS 域知识，本域不该
    知道。要过滤的调用方自己用 `hls.segments()` 问；而 TTL 恰恰**不能**过滤：只有
    `features.jsonl`、没有 HLS 段的 step 目录正是当前泄漏的那一类，它必须被看见。
    """
    step_ids = [step_id for step_id, _ in _step_id_dirs(task_id)]
    step_ids.sort()
    return step_ids


def ids(order: str = "id") -> List[int]:
    """存储根下的 task 子目录 id。存储根不存在返回 `[]`。

    Args:
        order: `"id"` 升序；`"mtime"` 按 `_latest_step_mtime()` 降序。

    Raises:
        ValueError: order 不在 ("id", "mtime") 内。

    只认数字目录名。正常情况下存储根里也只有数字 task 目录——非 id 目录（误建的、外部
    工具留下的）跳过，不报错。

    两种顺序都是事实（不是"安全默认"与"危险选项"的关系），选错只影响清单顺序、不会静默
    出错，故 order 给默认值是安全的——这也是它做成参数而不是拆两个函数的理由。

    ⚠ **`order="mtime"` 是近似值，仅供挑深扫候选**：它 ≈ 最后一次有产物落盘的时刻，但
    lab 临时件的增删同样会刷新它。绝不能拿它当时间戳对外——对外的时间一律取真实段 ts。
    只 stat 目录、不进目录读文件，成本 O(目录数)。无 step 子目录的 task 排序键取 0
    （排最后），但仍保留在结果里，由调用方深扫时丢弃。
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

    # mtime 降序；同 mtime 时 task_id 大者优先（元组整体逆序排，与旧实现同口径）
    keyed = [(_latest_step_mtime(task_id), task_id) for task_id in task_ids]
    keyed.sort(reverse=True)
    return [task_id for _, task_id in keyed]


def _latest_step_mtime(task_id: int) -> float:
    """该 task 「最后有人写东西」的近似时刻；无 step 子目录返回 0.0。

    取 **step 目录及其各域子目录** mtime 的最大值。

    **必须下钻到域子目录**：产物落在 `{step}/{domain}/` 里，写一个段只更新 `hls/` 的
    mtime，step 目录本身纹丝不动（它只在新建域目录那一刻变）。只 stat step 目录会让这个
    值退化成「该 step 首次落盘的时刻」，大屏历史的「最近」排序随之失准——这是目录隔离
    带来的连带影响，不下钻就会静默退化。

    多一层仍是 O(目录数)：域数 ≤ 3，且全程只 stat 目录、不进目录读文件。
    """
    mtimes: List[float] = []
    for _, step_dir in _step_id_dirs(task_id):
        for candidate in (step_dir, *_iterdir(step_dir)):
            try:
                if candidate.is_dir():
                    mtimes.append(candidate.stat().st_mtime)
            except OSError:  # 扫描期间被删：等同于没扫到
                continue
    return max(mtimes) if mtimes else 0.0


def purge_step(task_id: int, step_id: int) -> bool:
    """删除整个 step 目录（含**所有写者**的产物）；父 task 目录若因此变空，一并回收。

    Returns:
        step 目录是否被删除。目录本就不存在返回 False；删除失败记 warning 后返回 False。

    **只执行，不判断该不该删**：「重启 supersede」与「TTL 到期」是两个不同的判断，分别
    留在 `persistence/manager` 与 `persistence/workers/cleanup_worker`。

    **它删的是整个 step，不是某一个域**：`hls/`、`features/`、`lab/` 全部子目录一起消失。
    调用方必须知道这一点——历史上 `hls_strategy.purge_step_dir` 的 docstring 自述"只删
    HLS 产物"而实际 rmtree 整个目录，是一处删除者自述与行为不符的缺陷。**要只删一个域，
    到那个域自己的模块里找**：本模块是跨域的，不提供也不该知道域粒度的删除。

    **不加锁——已知缺口，见规范 §7.4 C7。** 这条原本的理由是「写者调它时仍在自己的目录
    锁内」，但那只对 supersede 成立：TTL 回收由 `cleanup_worker` 发起，它持的是 hls 的
    目录锁，而 `FeatureStore` 的 append 持的是 `store.py` 自己的 `_lock`——两把不同的锁，
    `rmtree` 与并发写之间零互斥。**单域锁挡不住跨域删除**。表现是某个域的目录删到一半，
    或写侧的 `create=True` 在 rmtree 之后把目录重建出来、留一个已被记账删除的僵尸 step，
    两种都不报错。目标形态：本函数取该 step 的独占锁，各域的写取共享锁。

    空 task 目录用 `rmdir` 回收：它对非空目录会安全失败，故无需先判空，也不存在"刚好有
    别的 step 正在建目录"的窗口问题（那种情况下 rmdir 失败，目录保留，正确）。
    """
    step_dir = _root.path(task_id, step_id)
    if not step_dir.exists():
        return False

    try:
        shutil.rmtree(step_dir)
    except OSError as e:
        logger.warning("[Storage] 删除 step 目录失败 %s: %s", step_dir, e)
        return False

    try:
        _root.path(task_id).rmdir()
    except OSError:
        # 还有别的 step / 已被并发删掉 / 无权限 —— 三种都无需处理，目录留着零成本
        pass
    return True
