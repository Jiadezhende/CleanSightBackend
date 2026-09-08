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

from app.services.step_store import store as step_store
from app.services.traceback import MediaToken, MediaTokenError

router = APIRouter(prefix="/media", tags=["media"])
logger = logging.getLogger(__name__)


def _resolve_media_path(task_id: int, step_id: int, filename: str) -> Path:
    """按 token 已校验的字段取产物路径。

    定位与 path traversal 防御都在 `store.find_file` —— 本层只把「拿不到」映射成
    HTTP 状态码，不碰存储根、不拼目录。

    Raises:
        HTTPException(404): 文件不存在、越界或文件名非法
    """
    candidate = step_store.find_file(task_id, step_id, filename)
    if candidate is None:
        logger.warning(
            "[Media] Rejected: task_id=%s step_id=%s filename=%s",
            task_id, step_id, filename,
        )
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
