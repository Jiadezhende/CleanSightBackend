"""run 目录 —— 分配与查询。`{root}/{task}/{step}/{run_id}/` 只由 `allocate` 建出来。

    run = runs.allocate(task_id, step_id)           # /api/start 在 lock_for 内调用
    run = runs.query(task_id, step_id)              # 最新可见 run；None → 404
    run = runs.query(task_id, step_id, run_id)      # 点名的 run；目录不在 → None
    runs.successor(run)                             # 同 step 下一个 run 的 run_id；最新的 → None

硬约束：

- **同一 step 的 `allocate` 必须串行**（调用方持 `lock_for`）：`run_id` 的严格递增靠它。
- **写者不建 run 目录**：本模块是唯一 `mkdir(parents=True)` 出产物目录的地方；域写口收
  `RunIdentity` 时只建域这一级，run 被回收后的迟到写入原子失败。
- **可见** = run 下任一域有主产物：`hls/metadata.json`（`insert_segment` 提交的最后一步）
  或 `inference/detections.jsonl`。只影响缺省 `run_id` 的查询。

设计见 `docs/update/20260927_STORAGE_RUN_DIR_PROPOSAL.md` §2、§4。
"""

from __future__ import annotations

import time
from typing import Optional

from app.types.run import RunIdentity

from . import _root
from .hls import _layout as _hls_layout
from .inference import _layout as _inference_layout

__all__ = ["allocate", "query", "successor"]


def allocate(task_id: int, step_id: int) -> RunIdentity:
    """分配一个新 run 并建出它的目录。

    `run_id = max(当前微秒, 该 step 已有最大 run_id + 1)`：时钟回拨也严格递增。

    Raises:
        OSError: 建目录失败（含 run 目录已存在——那说明分配没有串行）。
    """
    existing = _root.run_ids(task_id, step_id)
    run_id = time.time_ns() // 1000
    if existing:
        run_id = max(run_id, existing[-1] + 1)
    run = RunIdentity(task_id=task_id, step_id=step_id, run_id=run_id)
    _root.run_path(run).mkdir(parents=True)
    return run


def _visible(run: RunIdentity) -> bool:
    detections = _inference_layout.domain_dir(run) / _inference_layout.DETECTIONS_NAME
    return _hls_layout.metadata_path(run).exists() or detections.exists()


def query(task_id: int, step_id: int, run_id: Optional[int] = None) -> Optional[RunIdentity]:
    """查询一个 run。

    - `run_id` 给了：该 run 目录存在就返回，不存在（写错或已回收）返回 None。不做可见判断。
    - `run_id` 为空：按 `run_id` 降序返回第一个可见的 run；没有返回 None。
    """
    if run_id is not None:
        run = RunIdentity(task_id=task_id, step_id=step_id, run_id=run_id)
        return run if _root.run_path(run).is_dir() else None
    for candidate in reversed(_root.run_ids(task_id, step_id)):
        run = RunIdentity(task_id=task_id, step_id=step_id, run_id=candidate)
        if _visible(run):
            return run
    return None


def successor(run: RunIdentity) -> Optional[int]:
    """同 step 下紧接着 `run` 分配的那个 run 的 `run_id`；`run` 是最新的则 None。

    不做可见判断：取代关系看的是分配顺序，与有没有产物无关。
    """
    later = [r for r in _root.run_ids(run.task_id, run.step_id) if r > run.run_id]
    return later[0] if later else None
