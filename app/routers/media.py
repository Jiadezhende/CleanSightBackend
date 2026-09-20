"""
媒体访问层 `/media/*`

前后端物理隔离的关键：所有视频/JSON 资源走 token 化 HTTP 路由，
URL 不暴露文件系统路径，避免越权枚举。

路由：
    GET /media/segment/{token}    流式返回 MP4 段（fMP4 fragment）
    GET /media/init/{token}       返回 HLS fMP4 init segment（step 级共享）

Token 校验由 MediaToken（HMAC-SHA256 + 短 TTL）完成。
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Path as PathParam
from fastapi.responses import FileResponse

from app.services.traceback import MediaToken, MediaTokenError, SegmentFinder
from app.services.traceback.segment_finder import get_default_base_dir

router = APIRouter(prefix="/media", tags=["media"])
logger = logging.getLogger(__name__)


def _resolve_media_path(task_id: int, step_id: int, filename: str) -> Path:
    """根据 token 已校验的字段拼出绝对路径，并防止 path traversal。

    Raises:
        HTTPException(400): filename 含路径分隔符
        HTTPException(404): 文件不存在或越界
    """
    if "/" in filename or "\\" in filename or filename in (".", ".."):
        raise HTTPException(status_code=400, detail="Invalid filename")

    base = get_default_base_dir()
    finder = SegmentFinder(base)
    candidate = (finder.task_dir(task_id, step_id) / filename).resolve()

    # path traversal 防御：解析后的路径必须在 base_dir 内
    try:
        candidate.relative_to(base)
    except ValueError:
        logger.warning(
            "[Media] Path traversal denied: task_id=%s step_id=%s filename=%s",
            task_id, step_id, filename,
        )
        raise HTTPException(status_code=400, detail="Invalid path")

    if not candidate.exists() or not candidate.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return candidate


@router.get("/segment/{token}")
async def get_segment(token: str = PathParam(..., description="media segment token")):
    """流式返回单个 MP4 段。"""
    try:
        payload = MediaToken.default().verify(token, kind="segment")
    except MediaTokenError as e:
        logger.info("[Media] Segment token rejected: %s", e)
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    if not payload.filename.endswith(".mp4"):
        raise HTTPException(status_code=400, detail="Token does not point to a segment")

    path = _resolve_media_path(payload.task_id, payload.step_id, payload.filename)
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        headers={
            "Cache-Control": "private, max-age=60",
            "Content-Disposition": "inline",
        },
    )


@router.get("/init/{token}")
async def get_init(token: str = PathParam(..., description="media init segment token")):
    """返回 HLS fMP4 init segment（每 step 一份共享）。"""
    try:
        payload = MediaToken.default().verify(token, kind="init")
    except MediaTokenError as e:
        logger.info("[Media] Init token rejected: %s", e)
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    if not payload.filename.endswith("init.mp4"):
        raise HTTPException(status_code=400, detail="Token does not point to init segment")

    path = _resolve_media_path(payload.task_id, payload.step_id, payload.filename)
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": "inline",
        },
    )
