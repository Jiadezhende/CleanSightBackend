"""
媒体访问层 `/media/*`

前后端物理隔离的关键：所有视频/JSON 资源走 token 化 HTTP 路由，
URL 不暴露文件系统路径，避免越权枚举。

路由：
    GET /media/segment/{token}    流式返回 MP4 段（fMP4 fragment）
    GET /media/init/{token}       返回 HLS fMP4 init segment（step 级共享）

Token 校验由 MediaToken（HMAC-SHA256 + 短 TTL）完成。

**外部字符串从不进入路径拼接**：token 里的 filename 先由 `hls.parse_segment_name` /
`hls.parse_init_name` 解成身份键（`SegmentRef` / track），解不出就 400；路径一律由
`hls.segment_path` / `hls.init_path` 按落盘结构重建。故这里没有 `relative_to(base)`
那类事后越界检查——能拼出来的路径只可能落在该 step 的 `hls/` 域目录里。
"""

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException, Path as PathParam
from fastapi.responses import FileResponse

from app.services.traceback import MediaToken, MediaTokenError
from app.storage import hls

router = APIRouter(prefix="/media", tags=["media"])
logger = logging.getLogger(__name__)


def _reject(task_id: int, step_id: int, filename: str, detail: str) -> HTTPException:
    """token 里的 filename 不是合法产物名 —— 记一条 warning 并给出 400。

    签发侧（`MediaToken.sign`）已经拒过带分隔符的名字，走到这里说明 token 不是本服务签
    的正常流程产物（伪造、或签发方改了命名约定），故按可疑请求记 warning。
    """
    logger.warning(
        "[Media] Rejected filename: task_id=%s step_id=%s filename=%r",
        task_id, step_id, filename,
    )
    return HTTPException(status_code=400, detail=detail)


def _existing_file(path: Path) -> Path:
    """产物必须真的在盘上；不在就 404（区别于 400 的"名字非法"）。"""
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Media file not found")
    return path


@router.get("/segment/{token}")
async def get_segment(token: str = PathParam(..., description="media segment token")):
    """流式返回单个 MP4 段。"""
    try:
        payload = MediaToken.default().verify(token, kind="segment")
    except MediaTokenError as e:
        logger.info("[Media] Segment token rejected: %s", e)
        raise HTTPException(status_code=403, detail="Invalid or expired token")

    ref = hls.parse_segment_name(payload.filename)
    if ref is None:
        raise _reject(
            payload.task_id, payload.step_id, payload.filename,
            "Token does not point to a segment",
        )

    path = _existing_file(hls.segment_path(payload.task_id, payload.step_id, ref))
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

    # 判据是 `parse_init_name` 而不是 `endswith("init.mp4")` —— 后者放行 `evil_init.mp4`。
    track = hls.parse_init_name(payload.filename)
    if track is None:
        raise _reject(
            payload.task_id, payload.step_id, payload.filename,
            "Token does not point to init segment",
        )

    path = _existing_file(hls.init_path(payload.task_id, payload.step_id, track))
    return FileResponse(
        path=str(path),
        media_type="video/mp4",
        headers={
            "Cache-Control": "private, max-age=3600",
            "Content-Disposition": "inline",
        },
    )
