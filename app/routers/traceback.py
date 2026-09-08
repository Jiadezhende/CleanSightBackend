"""
追溯 API（`/traceback/*`）

提供任务 VOD 回放、任务时间轴打点两个核心接口。

数据底座：
- 落盘的一切经 `step_store`：给 `(task_id, step_id)` 拿 `Step` 句柄，段定位 / EXTINF
  时长 / init 判据 / VOD m3u8 都问它。本层不知道目录长什么样、文件叫什么名
- 媒体访问：HMAC token 化的 /media/* 路由（media_token + media router）

设计要点：
- 不再依赖 clean_task.source_ip —— 该字段会被业务侧覆写，无法作为可靠输入
- playlist/timeline 接口必填 step_id query 参数，仅返回该 step 的数据

**「从 alarm_id 反查视频段」不在本模块**（`/alarm/{id}/evidence` 与
`/alarm/{id}/playlist.m3u8` 已下线）：同一需求由 timeline + task 回放组合完成 ——
timeline 出告警打点的 `ts_ms`，前端在整 step 回放上 seek 过去。那是**帧级**定位，
比按段返回上下文窗口的旧实现更准，且不需要段列表出 `step_store`。
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


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------


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
# 接口 1: 任务 VOD playlist
# ---------------------------------------------------------------------------


def _build_vod_playlist(request: Request, step: Step, track: str) -> str:
    """构造该轨整段回放的 VOD m3u8 文本体。

    **本层只剩两件事：把段身份翻成 token 化 URL、把领域异常翻成状态码。** 备料
    （EXTINF 真值、滤在途段、判 init、算 TARGETDURATION）全在 `Step.vod_playlist`
    —— 那才是写错会静默的部分：骨架写错播放器立刻报错，备料写错表现为 hls.js 段尾
    停摆、缓冲洞。

    URI 形态经 `encode_uri` 注入：step_store 只写**段的身份**（文件名），token 化是
    访问控制、属本层。

    **不再收 `segs` 参数**：告警证据下线后只剩整轨回放一个调用方，而整轨可播段正是
    `Step.vod_playlist` 的默认取值 —— 段列表出包一趟再原样传回包，中间没有任何一个
    字段被读过。
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
        return step.vod_playlist(track, encode_uri=_url)
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
    # 「磁盘上这轨一个段都没有」→ 404「没这个 step」。`step.tracks` 与旧写法
    # （`segments(track, playable_only=False)` 判空）等价且同走一次目录扫描缓存；
    # 有段但全在途 → 由 vod_playlist 抛 StepNoPlayableSegments，措辞不同（见下）。
    if track not in step.tracks:
        raise NotFoundError(
            f"No {track} segments for task {task_id} step {step_id}",
            resource_type="Segments",
            resource_id=f"task={task_id},step={step_id},track={track}",
        )

    body = _build_vod_playlist(request, step, track)
    return PlainTextResponse(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# 接口 2: 任务时间轴打点
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
