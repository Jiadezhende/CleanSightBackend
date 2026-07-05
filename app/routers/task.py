"""
用于清洗任务控制
包括初始化、终止任务等功能
"""

import logging

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models import DBAlarm
from app.services.client.manager import client_manager
from app.utils.exceptions import DatabaseError

router = APIRouter(prefix="/task", tags=["task"])
logger = logging.getLogger(__name__)


@router.get("/{task_id}/alarms")
async def get_task_alarms(task_id: int):
    """
    获取指定任务的所有告警记录（始终查 DB）。

    告警由 AlarmWorker 实时异步写入 clean_alarm 表，活跃任务查 DB 与内存一致（秒级延迟）。

    Raises:
        DatabaseError: 数据库查询失败（由边界层 3 转换为 503）
    """
    db = next(get_db())
    try:
        try:
            rows = (
                db.query(DBAlarm)
                .filter(DBAlarm.task_id == int(task_id))
                .order_by(DBAlarm.create_time.desc())
                .all()
            )
        except SQLAlchemyError as e:
            raise DatabaseError(
                message=f"Failed to fetch alarms for task {task_id}",
                retryable=True,
                query=f"SELECT ... FROM clean_alarm WHERE task_id = {task_id}",
            ) from e

        alarms = []
        for r in rows:
            alarms.append(
                {
                    "alarm_id": r.alarm_id,
                    "task_id": r.task_id,
                    "step_id": r.step_id,
                    "step_name": r.step_name,
                    "alarm_type": r.alarm_type,
                    "severity": r.severity,
                    "message": r.message,
                    "resolved": bool(r.resolved) if r.resolved is not None else False,
                    "resolved_by": r.resolved_by,
                    "detected_at": int(r.detected_at) if r.detected_at is not None else None,  # type: ignore[arg-type]
                    "resolved_at": int(r.resolved_at) if r.resolved_at is not None else None,  # type: ignore[arg-type]
                }
            )

        return {"task_id": task_id, "total": len(alarms), "alarms": alarms}
    finally:
        db.close()


def _build_signals_10s(stream_summary: dict) -> dict:
    """把 CQ 的按流名汇总（{stream_name: {active,hit_count,max_conf}}）映射为
    前端 signals_10s（按 metric.value 键组织，含全量空模板）。

    metric 映射是 inference 展示知识，收敛在 router 装配层；CQ 只出纯流名汇总。
    """
    from app.services.inference.naming import get_task_metric_map

    metric_map = get_task_metric_map()
    _empty = {"active": False, "hit_count": 0, "max_conf": 0.0}
    out = {m.value: dict(_empty) for m in metric_map.values()}  # 空模板
    for stream_name, summ in stream_summary.items():             # 实时覆盖
        metric = metric_map.get(stream_name)
        if metric is not None:
            out[metric.value] = summ
    return out


def _empty_alarm_payload(task_id: int) -> dict:
    return {
        "task_id": task_id,
        "max_seq": 0,
        "signals_10s": _build_signals_10s({}),
        "alarms": [],
    }


def _build_task_alarm_message(cq, since_seq: int) -> dict:
    """装配前端实时告警消息：原子取告警增量 + 滑窗汇总，序列化域对象。"""
    alarms, max_seq = cq.get_alarm_snapshot(since_seq)  # 原子 (增量, max_seq)
    return {
        "task_id": cq.task_id,
        "max_seq": max_seq,
        "signals_10s": _build_signals_10s(cq.get_slide_window_summary()),
        "alarms": [
            {
                "seq": a.seq,
                "mode": a.mode,
                "metric": a.metric,
                "level": a.alarm_level,
                "message": a.alarm_message,
                "ts": int(a.timestamp),
            }
            for a in alarms
        ],
    }


@router.get("/message/{task_id}")
async def get_client_frontend_message(
    task_id: int,
    since_seq: int = Query(default=0),
):
    """
    获取指定 task 的前端实时告警消息（按 seq 增量）

    包含：当前状态、时序事件、各任务检测结果、最近5条内存告警。
    适合前端轮询（建议 1~2 Hz），用于告警提示等实时展示场景。

    与 GET /task/{task_id}/alarms 的区别：
    - 本接口：实时内存增量数据，按 task_id 查询
    - alarms 接口：持久化数据库历史记录
    """
    if since_seq < 0:
        raise HTTPException(status_code=400, detail="since_seq must be >= 0")

    cq = client_manager.get(task_id)  # 键即 task_id，O(1) 直取
    if cq is None:
        return _empty_alarm_payload(task_id)

    return _build_task_alarm_message(cq, since_seq)
