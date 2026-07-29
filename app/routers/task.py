"""
用于清洗任务控制
包括初始化、终止任务等功能
"""

import logging
from typing import Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models import DBAlarm, DBTask
from app.services.client.manager import client_manager
from app.services.traceback import SegmentFinder
from app.services.traceback.segment_finder import get_default_base_dir
from app.utils.exceptions import DatabaseError

router = APIRouter(prefix="/task", tags=["task"])
logger = logging.getLogger(__name__)

# 历史清单条数（大屏静态展示「最近 N 个」，无翻页语义故不做查询参数）
_HISTORY_LIMIT = 10
# 深扫上限：粗筛序里最多试这么多个 task 就收手，防存储目录病态（大量空 task 目录）
# 时把整个存储扫穿。正常情况下前 _HISTORY_LIMIT 个就收满了。
_HISTORY_SCAN_CAP = 30


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


# ---------------------------------------------------------------------------
# 大屏清单：在线任务 / 历史任务
#
# 两张清单都**只出参数、不出 URL**——前端拿参数自己拼播放地址：
#   /task/live    → task_id | source_ip → `WS /ai/video?task_id=` | `?client_id=`
#   /task/history → (task_id, step_id, track) → `GET /traceback/task/{task_id}/playlist.m3u8`
# ---------------------------------------------------------------------------


def _fetch_source_ips(task_ids: List[int]) -> Dict[int, Optional[str]]:
    """批量取 task_id → source_ip（单次 IN 查询）。

    DB 在这里是**锦上添花**：历史清单的存在性判定完全来自磁盘，source_ip 只是
    给大屏显示点位。故 DB 任何故障（含建连失败）都吞掉返回空映射，让清单降级为
    source_ip=null 照常返回，不 503——与 /traceback timeline 的降级策略一致。
    """
    if not task_ids:
        return {}

    db = None
    try:
        db = next(get_db())
        rows = (
            db.query(DBTask.task_id, DBTask.source_ip)
            .filter(DBTask.task_id.in_(task_ids))
            .all()
        )
        return {int(r.task_id): r.source_ip for r in rows}
    except Exception as exc:  # noqa: BLE001 —— 降级路径，不区分故障类型
        logger.warning("[TaskList] DB 不可用，历史清单降级为无 source_ip: %s", exc)
        return {}
    finally:
        if db is not None:
            db.close()


@router.get("/live")
def list_live_tasks():
    """在线任务清单（大屏用）。纯内存，零 DB、零磁盘。

    返回的字段即实时画面入参：
    - `task_id`   → `WS /ai/video?task_id={task_id}`：锁定**这一次 run**，run 结束即止
    - `source_ip` → `WS /ai/video?client_id={source_ip}`：跟随该**点位**当前 run，换任务自动跟

    `step_id` 仅供展示当前洗消阶段，不参与画面路由。

    注：本接口是 admin 页 `/admin-f3m8/clients` 的大屏版——同一份注册表快照，
    去掉队列深度等运维字段。
    """
    # 注册表是 COW 不可变 dict：原子读引用后迭代无需加锁
    tasks = [
        {
            "task_id": cq.task_id,
            "source_ip": cq.source_ip,
            "step_id": cq.step_id,
        }
        for cq in client_manager.snapshot().values()
    ]
    tasks.sort(key=lambda t: t["task_id"])
    return {"total": len(tasks), "tasks": tasks}


@router.get("/history")
def list_history_tasks():
    """历史任务清单（大屏用）：最近 10 个**已完成且能回放**的任务。无查询参数。

    「已完成」= 磁盘上有段（能播） **且** 不在活跃注册表里（跑完了）。刻意**不看**
    `clean_task.status`——该字段由平台业务侧写入，取值集合后端无从校验，拿它过滤
    等于把清单挂在一个未知字面量上，写错就静默变空。上述两个条件后端都是权威。

    返回的字段即历史画面入参：
        GET /traceback/task/{task_id}/playlist.m3u8?step_id={step_id}&track={track}
        GET /traceback/task/{task_id}/timeline?step_id={step_id}

    `track` **必须从 `steps[].tracks` 里挑**：playlist 的 track 默认 processed，
    而只落了 raw 的 step 照默认打过去就是 404。

    时间字段的粒度刻意压在 **step** 上——回放本身就是 step 粒度（playlist 必填
    step_id，跨 step 聚合不支持），且两个 step 之间可以隔任意长时间，任务级
    「min(start) ~ max(last)」会跨过中间空档，既不是任务时长也不对应任何可播放
    的东西。任务级只留 `latest_ms`（= max(steps[].last_segment_ms)）作排序键与
    「最近一次有画面」的展示值，不成对给 start，免得被读成连续区间。

    `last_segment_ms` 是最后一段的**起点**，不是结束时刻（差一个段长）；精确时长
    取 timeline 的 `duration_ms`（按 playlist EXTINF 算）。

    实现是两阶段，避免每次请求全盘扫段：目录 mtime 粗排挑候选 → 只对候选深扫拿
    真实段时间与轨道，收满 10 条即停。mtime 只用于挑候选，对外时间戳一律取真实
    段 ts。粗筛与深扫之间任务可能刚起/刚停，清单可能短暂含一个刚起的 run 或漏一个
    刚停的——大屏下一轮轮询自愈，不加锁。
    """
    finder = SegmentFinder(get_default_base_dir())
    active_ids = set(client_manager.snapshot().keys())

    tasks: List[dict] = []
    scanned = 0
    for task_id in finder.list_task_ids_by_recency():
        if len(tasks) >= _HISTORY_LIMIT or scanned >= _HISTORY_SCAN_CAP:
            break
        if task_id in active_ids:  # 还在跑 → 不算历史
            continue

        scanned += 1
        steps = finder.list_steps(task_id)
        if not steps:  # 目录在但没段（起流即失败）→ 点开是黑屏，不进清单
            continue

        tasks.append(
            {
                "task_id": task_id,
                "source_ip": None,  # 下方按页补
                "latest_ms": max(s.last_ts_us for s in steps) // 1000,
                "steps": [
                    {
                        "step_id": s.step_id,
                        "tracks": list(s.tracks),
                        "start_ms": s.first_ts_us // 1000,
                        "last_segment_ms": s.last_ts_us // 1000,
                    }
                    for s in steps
                ],
            }
        )

    # 粗筛序基于 mtime（近似），最终顺序按真实段时间戳重排一次
    tasks.sort(key=lambda t: (t["latest_ms"], t["task_id"]), reverse=True)

    source_ips = _fetch_source_ips([t["task_id"] for t in tasks])
    for t in tasks:
        t["source_ip"] = source_ips.get(t["task_id"])

    return {"tasks": tasks}
