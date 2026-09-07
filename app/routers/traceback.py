"""
追溯 API（`/traceback/*`）

提供告警证据回溯、任务 VOD 回放、任务时间轴打点三个核心接口。

数据底座：
- 落盘的一切经 `step_store`：给 `(task_id, step_id)` 拿 `Step` 句柄，段定位 / EXTINF
  时长 / init 判据 / VOD m3u8 都问它。本层不知道目录长什么样、文件叫什么名
- 媒体访问：HMAC token 化的 /media/* 路由（media_token + media router）

设计要点：
- 不再依赖 clean_task.source_ip —— 该字段会被业务侧覆写，无法作为可靠输入
- evidence 接口直接用 alarm 自带的 (task_id, step_id) 定位
- playlist/timeline 接口必填 step_id query 参数，仅返回该 step 的数据
"""

import logging
from typing import Any, Dict, List, Optional, Tuple, cast

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models import DBAlarm
from app.services.step_store import store as step_store
from app.services.step_store.store import (
    SegmentRef,
    Step,
    StepInitMissing,
    StepNoPlayableSegments,
)
from app.services.traceback import MediaToken
from app.services.traceback.media_token import MediaKind
from app.utils.exceptions import DatabaseError, NotFoundError, ValidationError

router = APIRouter(prefix="/traceback", tags=["traceback"])
logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 工具：detected_at 单位归一化
# ---------------------------------------------------------------------------


def _to_ms(detected_at: Optional[int]) -> int:
    """把 detected_at 归一到毫秒。

    - 平台若以秒（10 位整数）存入，乘 1000
    - 已是毫秒（13 位）原样返回
    - 微秒（16 位）则除以 1000
    """
    if detected_at is None:
        raise ValidationError("alarm.detected_at is null", field="detected_at")
    v = int(detected_at)
    if v <= 0:
        raise ValidationError(
            "alarm.detected_at must be positive", field="detected_at", value=str(v)
        )
    if v < 10**11:        # 秒级
        return v * 1000
    if v < 10**14:        # 毫秒级
        return v
    return v // 1000      # 微秒级或更高


def _segment_to_url(
    req: Request, step: Step, seg: SegmentRef, is_trigger: bool
) -> Dict[str, Any]:
    """把段引用打包为前端可消费结构（带 token 化 URL）。

    `(task_id, step_id)` 从 `step` 句柄取而不再从 `seg` 取 —— 它们本就是同一个值，
    段上再带一份只会让调用点去选「用哪个真源」。`is_trigger` 同理：那是查询上下文
    不是段属性，由 `segments_around()` 返回的下标算出来。
    """
    token = MediaToken.default().sign(
        task_id=step.task_id,
        step_id=step.step_id,
        filename=seg.filename,
        kind="segment",
    )
    base = str(req.base_url).rstrip("/")
    return {
        "url": f"{base}/media/segment/{token}",
        "filename": seg.filename,
        "ts_us": seg.ts_us,
        "ts_ms": seg.ts_ms,
        "is_trigger": is_trigger,
    }


def _clips(
    req: Request, step: Step, segs: List[SegmentRef], trigger_idx: int
) -> List[Dict[str, Any]]:
    return [
        _segment_to_url(req, step, s, is_trigger=(i == trigger_idx))
        for i, s in enumerate(segs)
    ]


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


def _fetch_alarm(alarm_id: int) -> Dict[str, Any]:
    """按 alarm_id 拉一条告警；不存在则 NotFoundError → 404"""
    db = next(get_db())
    try:
        try:
            row = db.query(DBAlarm).filter(DBAlarm.alarm_id == int(alarm_id)).first()
        except SQLAlchemyError as e:
            raise DatabaseError(
                message=f"Failed to fetch alarm {alarm_id}",
                retryable=True,
                query=f"SELECT ... FROM clean_alarm WHERE alarm_id = {alarm_id}",
            ) from e

        if row is None:
            raise NotFoundError(
                f"Alarm {alarm_id} not found",
                resource_type="Alarm",
                resource_id=str(alarm_id),
            )

        return {
            "alarm_id": int(row.alarm_id),
            "task_id": int(row.task_id),
            "step_id": int(row.step_id) if row.step_id is not None else None,  # type: ignore[arg-type]
            "step_name": row.step_name,
            "alarm_type": row.alarm_type,
            "severity": row.severity,
            "message": row.message,
            "detected_at": int(row.detected_at) if row.detected_at is not None else None,  # type: ignore[arg-type]
            "resolved": bool(row.resolved) if row.resolved is not None else False,
            "resolved_by": row.resolved_by,
            "resolved_at": int(row.resolved_at) if row.resolved_at is not None else None,  # type: ignore[arg-type]
        }
    finally:
        db.close()


def _fetch_task_alarms(task_id: int, step_id: Optional[int] = None) -> List[Dict[str, Any]]:
    """按 task_id [+step_id] 拉告警列表（用于 timeline）。

    step_id 不为 None 时仅返回该 step 的告警。
    """
    db = next(get_db())
    try:
        try:
            q = db.query(DBAlarm).filter(DBAlarm.task_id == int(task_id))
            if step_id is not None:
                q = q.filter(DBAlarm.step_id == int(step_id))
            rows = q.order_by(DBAlarm.detected_at.asc()).all()
        except SQLAlchemyError as e:
            raise DatabaseError(
                message=f"Failed to fetch alarms for task {task_id}",
                retryable=True,
            ) from e

        return [
            {
                "alarm_id": int(r.alarm_id),
                "alarm_type": r.alarm_type,
                "severity": r.severity,
                "message": r.message,
                "step_id": int(r.step_id) if r.step_id is not None else None,  # type: ignore[arg-type]
                "step_name": r.step_name,
                "detected_at": int(r.detected_at) if r.detected_at is not None else None,  # type: ignore[arg-type]
            }
            for r in rows
        ]
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 接口 1: 告警证据
# ---------------------------------------------------------------------------


@router.get("/alarm/{alarm_id}/evidence")
async def get_alarm_evidence(
    request: Request,
    alarm_id: int,
    n_before: int = Query(default=-1, ge=-1, le=20, description="触发段前上下文段数 (-1 用配置默认值)"),
    n_after: int = Query(default=-1, ge=-1, le=20, description="触发段后上下文段数 (-1 用配置默认值)"),
):
    """单条告警的双轨视频证据。

    通过 alarm 表自带的 (task_id, step_id) 直接定位文件，无需查 clean_task.source_ip。

    返回：
        {
          "alarm": {...},
          "raw_clips":       [{"url", "filename", "ts_us", "ts_ms", "is_trigger"}],
          "processed_clips": [...],
        }
    """
    from app.settings import settings as s

    if n_before < 0:
        n_before = s.traceback_context_before
    if n_after < 0:
        n_after = s.traceback_context_after

    alarm = _fetch_alarm(alarm_id)
    task_id = alarm["task_id"]
    step_id = alarm["step_id"]
    if step_id is None:
        raise NotFoundError(
            f"Alarm {alarm_id} has no step_id, cannot locate evidence",
            resource_type="Alarm",
            resource_id=str(alarm_id),
        )
    detected_ms = _to_ms(alarm["detected_at"])

    step = step_store.step(task_id, step_id)

    raw_segs, raw_trigger = step.segments_around(
        detected_ms, "raw", n_before, n_after
    )
    processed_segs, proc_trigger = step.segments_around(
        detected_ms, "processed", n_before, n_after
    )

    if not raw_segs and not processed_segs:
        # 告警存在但视频段都不在了（已清理 / 还未落盘）
        logger.warning(
            "[Traceback] No segments found for alarm_id=%s task_id=%s step_id=%s detected_ms=%s",
            alarm_id, task_id, step_id, detected_ms,
        )

    return {
        "alarm": alarm,
        "task_id": task_id,
        "step_id": step_id,
        "raw_clips": _clips(request, step, raw_segs, raw_trigger),
        "processed_clips": _clips(request, step, processed_segs, proc_trigger),
    }


# ---------------------------------------------------------------------------
# 接口 2: 任务 VOD playlist
# ---------------------------------------------------------------------------


def _build_vod_playlist(
    request: Request, step: Step, track: str, segs: List[SegmentRef]
) -> str:
    """构造 VOD m3u8 文本体。供 task 全量回放与 evidence 上下文回放复用。

    **本层只剩两件事：把段身份翻成 token 化 URL、把领域异常翻成状态码。** 备料
    （EXTINF 真值、滤在途段、判 init、算 TARGETDURATION）全在 `Step.vod_playlist`
    —— 那才是写错会静默的部分：骨架写错播放器立刻报错，备料写错表现为 hls.js 段尾
    停摆、缓冲洞。

    URI 形态经 `encode_uri` 注入：step_store 只写**段的身份**（文件名），token 化是
    访问控制、属本层。
    """
    base_url = str(request.base_url).rstrip("/")

    def _url(kind: str, filename: str) -> str:
        # step_store 的 kind 取值域（"segment" / "init"）与 MediaKind 恒等，故此处
        # cast 而非再做一次映射 —— 多一张映射表就多一处可以写错的地方。
        token = MediaToken.default().sign(
            task_id=step.task_id,
            step_id=step.step_id,
            filename=filename,
            kind=cast(MediaKind, kind),
        )
        return f"{base_url}/media/{kind}/{token}"

    try:
        return step.vod_playlist(track, segments=segs, encode_uri=_url)
    except StepInitMissing as e:
        # 缺 init = 旧格式产物或首段仍在 transcode，两者服务端都不可自愈 —— 故 503
        # 而非 404，让调用方按「此 step 不可回放」处理。
        raise HTTPException(
            status_code=503,
            detail={"error": "HLS init segment missing", "detail": str(e)},
        ) from e
    except StepNoPlayableSegments as e:
        raise HTTPException(status_code=404, detail="No playable segments yet") from e


# HEAD 与 GET 同注册：原生 HLS 播放栈（Safari/AVPlayer 等）在取 playlist 前会自动
# 发 HEAD 探可用性，这是浏览器媒体栈行为、前端 JS 拦不住。FastAPI 的 APIRoute 不像
# Starlette 原生 Route 那样给 GET 自动补 HEAD，漏注册即 405——既不合 RFC 9110，又会
# 被 Gateway 反扫描当扫描特征计数（阈值 10 次/300s → 封 IP 1h）。
# body 由 h11 在传输层抑制，Content-Length 仍为真值，handler 照常执行以给出 200/404。
@router.api_route(
    "/task/{task_id}/playlist.m3u8",
    methods=["GET", "HEAD"],
    response_class=PlainTextResponse,
    responses={
        200: {
            "content": {"application/vnd.apple.mpegurl": {}},
            "description": "Generated VOD m3u8",
        }
    },
)
async def get_task_playlist(
    request: Request,
    task_id: int,
    step_id: int = Query(..., description="洗消步骤 id（必填，仅返回该 step 的回放）"),
    track: str = Query(default="processed", pattern="^(raw|processed)$"),
):
    """单个洗消步骤的完整回放 VOD m3u8（动态生成，带 #EXT-X-ENDLIST）。

    仅返回 (task_id, step_id) 对应目录下的段。任务级跨 step 聚合本期不支持。

    相比直接 serve 落盘 LIVE playlist：
    - 保证 VOD 完整性（即使任务未封档）
    - URL 走 token 化 /media/segment/*，不暴露文件系统路径
    """
    step = step_store.step(task_id, step_id)
    # playable_only=False 保留原判据：磁盘上一个段都没有 → 404「没这个 step」；
    # 有段但全在途 → 由 vod_playlist 抛 StepNoPlayableSegments，措辞不同（见下）。
    segs = step.segments(track, playable_only=False)
    if not segs:
        raise NotFoundError(
            f"No {track} segments for task {task_id} step {step_id}",
            resource_type="Segments",
            resource_id=f"task={task_id},step={step_id},track={track}",
        )

    body = _build_vod_playlist(request, step, track, segs)
    return PlainTextResponse(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.api_route(  # HEAD 同注册，理由见上方 /task/{task_id}/playlist.m3u8
    "/alarm/{alarm_id}/playlist.m3u8",
    methods=["GET", "HEAD"],
    response_class=PlainTextResponse,
    responses={
        200: {
            "content": {"application/vnd.apple.mpegurl": {}},
            "description": "Evidence VOD m3u8 (trigger ± context)",
        }
    },
)
async def get_alarm_evidence_playlist(
    request: Request,
    alarm_id: int,
    track: str = Query(default="processed", pattern="^(raw|processed)$"),
    n_before: int = Query(default=-1, ge=-1, le=20),
    n_after: int = Query(default=-1, ge=-1, le=20),
):
    """单条告警证据回放的 VOD m3u8（trigger 段 + 前后上下文）。

    fMP4 段必须经 m3u8 + init segment 拼装才能由浏览器原生解码，因此 admin/lab
    端的「告警证据」播放走该接口而非裸 /media/segment/{token}。
    """
    from app.settings import settings as s

    if n_before < 0:
        n_before = s.traceback_context_before
    if n_after < 0:
        n_after = s.traceback_context_after

    alarm = _fetch_alarm(alarm_id)
    task_id = alarm["task_id"]
    step_id = alarm["step_id"]
    if step_id is None:
        raise NotFoundError(
            f"Alarm {alarm_id} has no step_id, cannot locate evidence",
            resource_type="Alarm",
            resource_id=str(alarm_id),
        )
    detected_ms = _to_ms(alarm["detected_at"])

    step = step_store.step(task_id, step_id)
    segs, _trigger_idx = step.segments_around(detected_ms, track, n_before, n_after)
    if not segs:
        raise NotFoundError(
            f"No {track} segments around alarm {alarm_id}",
            resource_type="Segments",
            resource_id=f"alarm={alarm_id},track={track}",
        )

    body = _build_vod_playlist(request, step, track, segs)
    return PlainTextResponse(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# 接口 3: 任务时间轴打点
# ---------------------------------------------------------------------------


def _step_duration_ms(step: Step) -> Tuple[int, int, int]:
    """返回 (start_ms, end_ms, duration_ms)。无段时返回 (0, 0, 0)。

    µs → ms 的换算留在此处：`Step.time_bounds_us` 产出微秒（与段文件名 ts_us 同单位），
    毫秒是本接口对前端的表示口径。
    """
    bounds = step.time_bounds_us
    if bounds is None:
        return 0, 0, 0
    start_us, end_us = bounds
    return start_us // 1000, end_us // 1000, max(0, (end_us - start_us) // 1000)


@router.get("/task/{task_id}/timeline")
async def get_task_timeline(
    task_id: int,
    step_id: int = Query(..., description="洗消步骤 id（必填，仅返回该 step 的事件）"),
):
    """单个洗消步骤的时间轴打点（前端在视频进度条上叠加告警标记）。

    仅扫 `{task_id}/{step_id}/` 目录的段、仅取该 step 的告警事件。

    告警事件来自 DB；DB 不可用时退化为空 events（仍返回段时长），不 503，
    DB 恢复后自动恢复告警标记。

    返回：
        {
          "task_id": ...,
          "step_id": ...,
          "start_ms": ...,
          "end_ms": ...,
          "duration_ms": ...,
          "events": [
             {"ts_ms": ..., "type": "alarm", "alarm_id": ..., "severity": ..., "alarm_type": ..., "message": ...}
          ]
        }
    """
    start_ms, end_ms, duration_ms = _step_duration_ms(
        step_store.step(task_id, step_id)
    )

    # 段时长来自磁盘，告警事件来自 DB。DB 不可用时退化为「无告警标记」的时间轴，
    # 不让整条加载链路 503；DB 恢复后自动重新带回标记（自愈，无需切换任何开关）。
    events: List[Dict[str, Any]] = []
    try:
        alarms = _fetch_task_alarms(task_id, step_id=step_id)
    except DatabaseError:
        logger.warning(
            "[Timeline] DB 不可用，task=%s step=%s 退化为无告警时间轴", task_id, step_id
        )
        alarms = []

    for a in alarms:
        if a["detected_at"] is None:
            continue
        events.append(
            {
                "ts_ms": _to_ms(a["detected_at"]),
                "type": "alarm",
                "alarm_id": a["alarm_id"],
                "alarm_type": a["alarm_type"],
                "severity": a["severity"],
                "step_id": a["step_id"],
                "step_name": a["step_name"],
                "message": a["message"],
            }
        )

    events.sort(key=lambda e: e["ts_ms"])

    return {
        "task_id": task_id,
        "step_id": step_id,
        "start_ms": start_ms,
        "end_ms": end_ms,
        "duration_ms": duration_ms,
        "events": events,
    }
