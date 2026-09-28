"""一次运行（run）的身份：`RunIdentity(task_id, step_id, run_id)`。

    from app.types.run import RunIdentity
    run = runs.allocate(task_id, step_id)     # 活着的 run（/api/start）
    run = runs.query(task_id, step_id)        # 盘上的 run（读侧、离线）

- **只含身份**，不含路径与派生量：盘上位置 `{root}/{task}/{step}/{run_id}/` 由存储层按它解析。
- **调用方不自己构造**：来源只有 `app.storage.runs` 的 `allocate` / `query`，两者给出的 `run_id`
  都一定有值。「是不是同一个 run」按值比较。
- `run_id` 是分配时刻的 epoch 毫秒，同一 step 内严格递增。

stdlib only。设计见 `docs/update/20260927_STORAGE_RUN_DIR_PROPOSAL.md` §2。
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RunIdentity:
    task_id: int
    step_id: int
    run_id: int
