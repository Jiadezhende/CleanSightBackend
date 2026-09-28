"""读侧入口处解析一次 run：之后整个请求只用这个 `RunIdentity`。

    run = resolve_run(task_id, step_id, run_id)   # 点名的 run 不在 → 404；缺省且无可见 run → None

缺省 `run_id` 时返回 None 而不是 404：老前端不带 `run_id`，各端点对「这个 step 没数据」原有的
响应（404 / 全 0）保持不变，由调用方按原样处理。
"""

from typing import Optional

from app.types.run import RunIdentity
from app.storage import runs
from app.types.exceptions import NotFoundError


def resolve_run(task_id: int, step_id: int, run_id: Optional[int]) -> Optional[RunIdentity]:
    """`runs.query` + 点名不中的 404。"""
    run = runs.query(task_id, step_id, run_id)
    if run is None and run_id is not None:
        raise NotFoundError(
            f"Run {run_id} not found for task {task_id} step {step_id} (wrong id, or reclaimed by TTL)",
            resource_type="Run",
            resource_id=f"task={task_id},step={step_id},run={run_id}",
        )
    return run
