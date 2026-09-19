"""
追溯 API（`/traceback/*`）

提供任务 VOD 回放、任务时间轴打点两个接口。

数据底座：
- task_id + step_id → 落盘目录：`{root}/{task_id}/{step_id}/hls/`（`app.storage.hls` 域）
- 段枚举与 EXTINF：`hls.list_playable_segments`（一次扫盘同时给出可播段与逐段真时长）
- 清单骨架：`app.services.utils.vod_playlist.render_vod`（URI 与时长来源归本层）
- 媒体访问：HMAC token 化的 /media/* 路由（media_token + media router）

设计要点：
- 不再依赖 clean_task.source_ip —— 该字段会被业务侧覆写，无法作为可靠输入
- playlist/timeline 接口必填 step_id query 参数，仅返回该 step 的数据
"""

import logging
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models import DBAlarm
from app.services.traceback import MediaToken
from app.services.utils.vod_playlist import VodEntry, render_vod
from app.storage import hls
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


def _build_vod_playlist(
    request: Request,
    task_id: int,
    step_id: int,
    track: str,
) -> str:
    """构造 VOD m3u8 文本体（供 `get_task_playlist` 使用）。

    分工：**可播段与逐段 EXTINF 是事实**，由 `hls.list_playable_segments` 出；**m3u8 骨架**
    由 `vod_playlist.render_vod` 出；本函数只负责把段与 init 换成 token 化 URI——URI 长什么样
    是协议层的事，数据层与骨架函数都对 `MediaToken` 零认知。

    - init.mp4 缺失 → 抛 503（fMP4 无 init 段无法播放）
    - 一个可播段都没有（段全在途 / 登记失败）→ 抛 404
    """
    if not hls.init_path(task_id, step_id, track).exists():
        # 正常落盘的 step 必有 init（首段 transcode 时产出）。缺 init 只剩两种可能：
        # ① 段是 {track}_init.mp4 命名之前的旧格式产物——不支持，也不提供迁移；
        # ② 首段正在 transcode 途中（窗口极短）。
        # 两者服务端都无法自愈，故 503 而非 404，让调用方按「此 step 不可回放」处理。
        raise HTTPException(
            status_code=503,
            detail={
                "error": "HLS init segment missing",
                "detail": (
                    f"{track}_init.mp4 not found for task {task_id} step {step_id}. "
                    "This step is either mid-transcode or written in an unsupported "
                    "legacy layout; it cannot be played back."
                ),
            },
        )

    # VOD 时长唯一真值源 = 写入侧 playlist 的 EXTINF；`list_playable_segments` 只读回、不
    # 重新推导、无第二兜底。不在清单里的段是在途段或登记失败的段，它同时把它们滤掉——避免
    # 回放出现与 fmp4 tfdt 累计对不上的"估算"行，导致 hls.js MSE 缓冲洞。
    playable = hls.list_playable_segments(task_id, step_id, track)
    if not playable:
        raise HTTPException(status_code=404, detail="No playable segments yet")

    base_url = str(request.base_url).rstrip("/")
    signer = MediaToken.default()

    init_token = signer.sign(
        task_id=task_id, step_id=step_id, filename=hls.init_name(track), kind="init",
    )
    entries = [
        VodEntry(
            uri=(
                f"{base_url}/media/segment/"
                + signer.sign(
                    task_id=task_id,
                    step_id=step_id,
                    filename=hls.segment_name(s.ref),
                    kind="segment",
                )
            ),
            duration_s=s.duration_s,
        )
        for s in playable
    ]
    return render_vod(entries, map_uri=f"{base_url}/media/init/{init_token}")


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
    # 两档要分开：**盘上一个段都没有** = 这个 step 没这条轨（404 NotFoundError），
    # **有段但一个都还不可播** = 段全在途，稍后重试就有（404 "No playable segments yet"，
    # 在 `_build_vod_playlist` 里抛）。合成一档会让前端分不清"挑错 track"和"再等等"。
    if not hls.list_segments(task_id, step_id, track):
        raise NotFoundError(
            f"No {track} segments for task {task_id} step {step_id}",
            resource_type="Segments",
            resource_id=f"task={task_id},step={step_id},track={track}",
        )

    body = _build_vod_playlist(request, task_id, step_id, track)
    return PlainTextResponse(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# 接口 2: 任务时间轴打点
# ---------------------------------------------------------------------------


def _step_duration_ms(task_id: int, step_id: int) -> Tuple[int, int, int]:
    """返回 (start_ms, end_ms, duration_ms)。无段时返回 (0, 0, 0)。

    end_ms 必须取 max(seg.ts + EXTINF)，而不是 max(seg.ts) —— 后者会漏掉最后一段
    自身长度。EXTINF 是 hls.js / fragment 媒体时长的同源真值，对齐到它才能保证
    lab 页面顶部"时长 / 进度条右端"和 <video>.duration 一致。

    在途段（mp4v 已落、transcode+append 未完成）在 playlist 里查不到 EXTINF，
    由 `hls.list_playable_segments` 一并滤掉 —— 与 `_build_vod_playlist` 同源同策略。
    raw / processed 双轨都纳入，取并集的最早起点和最晚终点。
    """
    start_us: Optional[int] = None
    end_us: Optional[int] = None
    for track in hls.TRACKS:
        for seg in hls.list_playable_segments(task_id, step_id, track):
            seg_end_us = seg.ref.ts_us + int(round(seg.duration_s * 1_000_000))
            if start_us is None or seg.ref.ts_us < start_us:
                start_us = seg.ref.ts_us
            if end_us is None or seg_end_us > end_us:
                end_us = seg_end_us
    if start_us is None or end_us is None:
        return 0, 0, 0
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
    start_ms, end_ms, duration_ms = _step_duration_ms(task_id, step_id)

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
