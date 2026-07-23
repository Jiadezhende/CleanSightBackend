"""
追溯 API（`/traceback/*`）

提供告警证据回溯、任务 VOD 回放、任务时间轴打点三个核心接口。

数据底座：
- task_id + step_id → 落盘目录：`{base_dir}/{task_id}/{step_id}/`
- 段定位：文件名 ts_us 二分查找（segment_finder）
- 媒体访问：HMAC token 化的 /media/* 路由（media_token + media router）

设计要点：
- 不再依赖 clean_task.source_ip —— 该字段会被业务侧覆写，无法作为可靠输入
- evidence 接口直接用 alarm 自带的 (task_id, step_id) 定位
- playlist/timeline 接口必填 step_id query 参数，仅返回该 step 的数据
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models import DBAlarm
from app.services.traceback import MediaToken, SegmentFinder
from app.services.traceback.segment_finder import SegmentRef, get_default_base_dir
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


def _segment_to_url(req: Request, finder: SegmentFinder, seg: SegmentRef) -> Dict[str, Any]:
    """把段引用打包为前端可消费结构（带 token 化 URL）"""
    token = MediaToken.default().sign(
        task_id=seg.task_id,
        step_id=seg.step_id,
        filename=seg.filename,
        kind="segment",
    )
    base = str(req.base_url).rstrip("/")
    return {
        "url": f"{base}/media/segment/{token}",
        "filename": seg.filename,
        "ts_us": seg.ts_us,
        "ts_ms": seg.ts_ms,
        "is_trigger": seg.is_trigger,
    }


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

    finder = SegmentFinder(get_default_base_dir())

    raw_segs = finder.find(task_id, step_id, detected_ms, "raw", n_before, n_after)
    processed_segs = finder.find(
        task_id, step_id, detected_ms, "processed", n_before, n_after
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
        "raw_clips": [_segment_to_url(request, finder, s) for s in raw_segs],
        "processed_clips": [_segment_to_url(request, finder, s) for s in processed_segs],
    }


# ---------------------------------------------------------------------------
# 接口 2: 任务 VOD playlist
# ---------------------------------------------------------------------------


def _parse_existing_playlist(playlist_path: Path) -> Dict[str, float]:
    """解析现有 LIVE m3u8，提取 filename → duration 映射。

    格式约定：每个段对应一行 `#EXTINF:<dur>,` 紧接一行 `<filename>`。
    返回字典；解析失败或文件不存在返回空字典。
    """
    if not playlist_path.exists():
        return {}
    durations: Dict[str, float] = {}
    try:
        with playlist_path.open("r", encoding="utf-8") as f:
            lines = f.readlines()
    except OSError as e:
        logger.warning("[Traceback] Failed to read playlist %s: %s", playlist_path, e)
        return {}

    pending_dur: Optional[float] = None
    for raw in lines:
        line = raw.strip()
        if line.startswith("#EXTINF:"):
            try:
                # "#EXTINF:1.234,"
                dur_str = line[len("#EXTINF:") :].rstrip(",").strip()
                pending_dur = float(dur_str)
            except ValueError:
                pending_dur = None
        elif line and not line.startswith("#"):
            if pending_dur is not None:
                durations[line] = pending_dur
            pending_dur = None
    return durations


def _build_vod_playlist(
    request: Request,
    finder: SegmentFinder,
    task_id: int,
    step_id: int,
    track: str,
    segs: List[SegmentRef],
) -> str:
    """构造 VOD m3u8 文本体。供 task 全量回放与 evidence 上下文回放复用。

    - 要求 segs 已经按时序排序、非空
    - 调用方负责处理 segs 为空时的 404
    - init.mp4 缺失 → 抛 503（fMP4 无 init 段无法播放）
    - segs 经 playlist 过滤后为空（全部为在途段）→ 抛 404
    """
    task_dir = finder.task_dir(task_id, step_id)
    init_path = task_dir / "init.mp4"
    if not init_path.exists():
        raise HTTPException(
            status_code=503,
            detail={
                "error": "HLS init segment missing",
                "detail": (
                    f"init.mp4 not found for task {task_id} step {step_id}. "
                    "Historical segments must be migrated via "
                    "scripts/transcode_segments_to_h264.py before HLS playback."
                ),
            },
        )

    playlist_path = task_dir / f"{track}_playlist.m3u8"
    real_durations = _parse_existing_playlist(playlist_path)

    # VOD 时长唯一真值源 = 写入侧 playlist 的 EXTINF（退化段的兜底也只在写入侧的 eff_fps
    # 里，见 hls_strategy._DEGENERATE_FALLBACK_FPS）。此处只读回、不重新推导、无第二兜底。
    # 不在 playlist 中的段视为在途段（mp4v 已落但 transcode+append 未完成），过滤掉——避免
    # 回放出现与 fmp4 tfdt 累计对不上的"估算"行，导致 hls.js MSE 缓冲洞。
    segs = [s for s in segs if s.filename in real_durations]
    if not segs:
        raise HTTPException(status_code=404, detail="No playable segments yet")

    # 上一步已保证 real_durations 非空（segs ⊆ real_durations 且非空），max() 无需 default。
    target_duration = max(int(round(max(real_durations.values()))), 1)

    base_url = str(request.base_url).rstrip("/")
    init_token = MediaToken.default().sign(
        task_id=task_id, step_id=step_id, filename="init.mp4", kind="init",
    )

    lines: List[str] = [
        "#EXTM3U",
        "#EXT-X-VERSION:7",
        "#EXT-X-PLAYLIST-TYPE:VOD",
        f"#EXT-X-TARGETDURATION:{target_duration}",
        "#EXT-X-MEDIA-SEQUENCE:0",
        f'#EXT-X-MAP:URI="{base_url}/media/init/{init_token}"',
    ]
    for s in segs:
        dur = real_durations[s.filename]
        token = MediaToken.default().sign(
            task_id=s.task_id, step_id=s.step_id, filename=s.filename, kind="segment",
        )
        lines.append(f"#EXTINF:{dur:.3f},")
        lines.append(f"{base_url}/media/segment/{token}")
    lines.append("#EXT-X-ENDLIST")
    return "\n".join(lines) + "\n"


@router.get(
    "/task/{task_id}/playlist.m3u8",
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
    finder = SegmentFinder(get_default_base_dir())
    segs = finder.list_segments(task_id, step_id, track)
    if not segs:
        raise NotFoundError(
            f"No {track} segments for task {task_id} step {step_id}",
            resource_type="Segments",
            resource_id=f"task={task_id},step={step_id},track={track}",
        )

    body = _build_vod_playlist(request, finder, task_id, step_id, track, segs)
    return PlainTextResponse(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


@router.get(
    "/alarm/{alarm_id}/playlist.m3u8",
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

    finder = SegmentFinder(get_default_base_dir())
    segs = finder.find(task_id, step_id, detected_ms, track, n_before, n_after)
    if not segs:
        raise NotFoundError(
            f"No {track} segments around alarm {alarm_id}",
            resource_type="Segments",
            resource_id=f"alarm={alarm_id},track={track}",
        )

    body = _build_vod_playlist(request, finder, task_id, step_id, track, segs)
    return PlainTextResponse(
        content=body,
        media_type="application/vnd.apple.mpegurl",
        headers={"Cache-Control": "no-store"},
    )


# ---------------------------------------------------------------------------
# 接口 3: 任务时间轴打点
# ---------------------------------------------------------------------------


def _step_duration_ms(
    finder: SegmentFinder, task_id: int, step_id: int
) -> Tuple[int, int, int]:
    """返回 (start_ms, end_ms, duration_ms)。无段时返回 (0, 0, 0)。

    end_ms 必须取 max(seg.ts + EXTINF)，而不是 max(seg.ts) —— 后者会漏掉最后一段
    自身长度。EXTINF 是 hls.js / fragment 媒体时长的同源真值，对齐到它才能保证
    lab 页面顶部"时长 / 进度条右端"和 <video>.duration 一致。

    在途段（mp4v 已落、transcode+append 未完成）在 playlist 里查不到 EXTINF，
    跳过 —— 与 `_build_vod_playlist` 的过滤策略保持一致。raw / processed 双轨都
    纳入，取并集的最早起点和最晚终点。
    """
    task_dir = finder.task_dir(task_id, step_id)
    start_us: Optional[int] = None
    end_us: Optional[int] = None
    for track in ("raw", "processed"):
        durations = _parse_existing_playlist(task_dir / f"{track}_playlist.m3u8")
        if not durations:
            continue
        for s in finder.list_segments(task_id, step_id, track):
            dur = durations.get(s.filename)
            if dur is None:
                continue
            seg_end_us = s.ts_us + int(round(dur * 1_000_000))
            if start_us is None or s.ts_us < start_us:
                start_us = s.ts_us
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
    finder = SegmentFinder(get_default_base_dir())
    start_ms, end_ms, duration_ms = _step_duration_ms(finder, task_id, step_id)

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
