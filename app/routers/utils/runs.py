"""读侧入口处解析一次 run：之后整个请求只用这个 `RunIdentity`。

    run = resolve_run(task_id, step_id, run_id)   # 点名的 run 不在 → 404；缺省且无可见 run → None
    if run is None:
        raise no_run(task_id, step_id)            # 端点必须有 run 时，None 分支的 404
    run, tl = resolve_timeline(task_id, step_id, run_id, track)   # 再要求该轨有段，否则 404
    run = resolve_media_run(payload)              # /media/*：token 锁定的 run，不在 → HTTP 404

缺省 `run_id` 时 `resolve_run` 返回 None 而不是 404：老前端不带 `run_id`，各端点对「这个 step
没数据」原有的响应（404 / 全 0）保持不变，由调用方按原样处理。
"""

from typing import Optional, Tuple

from fastapi import HTTPException

from app.types.run import RunIdentity
from app.storage import hls, runs
from app.types.exceptions import NotFoundError

from .media_token import MediaTokenPayload


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


def no_run(task_id: int, step_id: int) -> NotFoundError:
    """`resolve_run` 缺省 `run_id` 且该 step 无可见 run 时，要求必须有 run 的端点抛这个。"""
    return NotFoundError(
        f"no visible run for task {task_id} step {step_id}",
        resource_type="Run", resource_id=f"task={task_id},step={step_id}",
    )


def resolve_timeline(
    task_id: int, step_id: int, run_id: Optional[int], track: str
) -> Tuple[RunIdentity, hls.MediaTimeline]:
    """`resolve_run` + 该轨媒体轴；无可见 run 或该轨无段 → 404（Segments）。点名不中的 404 同 `resolve_run`。"""
    run = resolve_run(task_id, step_id, run_id)
    timeline = hls.query_timeline(run, track) if run is not None else hls.MediaTimeline([])
    if not timeline:
        raise NotFoundError(
            f"No {track} segments for task {task_id} step {step_id}",
            resource_type="Segments",
            resource_id=f"task={task_id},step={step_id},track={track}",
        )
    return run, timeline


def resolve_media_run(payload: MediaTokenPayload) -> RunIdentity:
    """token 锁定的 run（缺 `run_id` 的旧 token 按最新可见 run）；run 已回收或不存在 → 404。

    不并进 `resolve_run`：媒体路由对「run 不在」一律回 `HTTPException` 的
    "Media file not found"，不分点名 / 缺省，也不走 `NotFoundError` 的结构化错误体。
    """
    run = runs.query(payload.task_id, payload.step_id, payload.run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Media file not found")
    return run
