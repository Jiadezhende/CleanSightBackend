import logging

from fastapi import APIRouter, Query
from pydantic import BaseModel

from app.routers.health import get_health_monitor
from app.services import ai
from app.services.stream import stream_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/inspection", tags=["inspection"])


class RTSPStreamConfig(BaseModel):
    """RTSP 流配置"""

    client_id: str
    rtsp_url: str
    fps: int = 30  # 固定帧率


# 旧的 _stream_capture_worker 已移至 app.services.stream_service


@router.post("/start_rtsp_stream")
async def start_rtsp_stream(config: RTSPStreamConfig):
    """
    启动 RTSP 流捕获

    ⚠️ 过渡接口：建议使用 POST /api/start 代替

    注意：此接口只启动流，不加载任务。需要单独调用 /ai/load_task

    异常处理：让异常向上传播到边界层 3（FastAPI全局处理器）
    - StreamConnectionError: 流连接失败（503）
    - FFmpegError: FFmpeg 启动失败（500）

    POST /inspection/start_rtsp_stream
    Body: {"client_id": "xxx", "rtsp_url": "rtsp://localhost:8554/live/stream", "fps": 30}
    """
    client_id = config.client_id
    ai.set_stream_url(client_id, config.rtsp_url)

    # 启动流（可能抛出 StreamConnectionError 或 FFmpegError）
    # 这些异常将被 FastAPI 全局处理器捕获（边界层 3）
    stream_service.start_stream(
        client_id=client_id, stream_url=config.rtsp_url, fps=config.fps, protocol="RTSP"
    )

    return {"status": "success", "message": f"RTSP 流捕获已启动 for {client_id}"}


@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(..., description="客户端ID")):
    """
    停止 RTSP 流捕获（完整清理）

    ⚠️ 过渡接口：建议使用 POST /api/terminate 代替

    职责边界：
    - API 层负责接收用户请求
    - 清理协调由 GlobalHealthMonitor 负责
    - 通过 cleanup_client() 执行统一的 3 步清理

    执行完整清理流程：
    1. 停止解码器（FFmpeg 进程）
    2. 落盘残余数据（通过 InferenceManager）
    3. 清理 ClientManager

    POST /inspection/stop_rtsp_stream?client_id=xxx
    """
    logger.info(f"[stop_rtsp_stream] Stopping stream: {client_id}")

    # 路由清理到健康监控（统一入口）
    monitor = get_health_monitor()
    if monitor:
        result = monitor.cleanup_client(
            client_id=client_id, reason="RTSP stream stop request"
        )
    else:
        # Fallback: 健康监控未初始化，执行手动清理
        logger.warning(
            "[stop_rtsp_stream] Health monitor not initialized, using fallback cleanup"
        )
        result = _manual_cleanup_fallback(client_id)

    # 调整返回格式以匹配原有 API
    if result["errors"]:
        status = "partial_success"
        logger.warning(
            f"[stop_rtsp_stream] Completed with errors: {client_id} - {result['errors']}"
        )
    else:
        status = "success"

    return {
        "status": status,
        "message": f"RTSP 流捕获已停止 for {client_id}",
        "cleanup_details": result,
    }


def _manual_cleanup_fallback(client_id: str):
    """Fallback cleanup when health monitor not available

    职责边界：
    - 仅在健康监控未初始化时使用
    - 执行与 cleanup_client() 相同的 3 步清理
    - 生产环境不应触发此路径（说明初始化有问题）
    """
    from app.services.client.manager import client_manager

    result = {
        "client_id": client_id,
        "reason": "RTSP stream stop (fallback)",
        "decoder_stopped": False,
        "data_flushed": False,
        "client_cleaned": False,
        "errors": [],
    }

    # 1. 停止解码器
    try:
        if stream_service.has_stream(client_id):
            stream_service.stop_stream(client_id)
            result["decoder_stopped"] = True
            logger.info(f"[stop_rtsp_stream_fallback] Decoder stopped: {client_id}")
    except Exception as e:
        result["errors"].append(f"decoder: {e}")
        logger.error(
            f"[stop_rtsp_stream_fallback] Failed to stop decoder: {client_id} - {e}"
        )

    # 2. 落盘残余数据
    try:
        ai.remove_client(client_id)
        result["data_flushed"] = True
        logger.info(f"[stop_rtsp_stream_fallback] Data flushed: {client_id}")
    except Exception as e:
        result["errors"].append(f"flush: {e}")
        logger.error(
            f"[stop_rtsp_stream_fallback] Failed to flush data: {client_id} - {e}"
        )

    # 3. 清理 ClientManager
    try:
        if client_manager.has_client(client_id):
            removal_result = client_manager.remove_client(client_id, cleanup=True)
            result["client_cleaned"] = removal_result["removed"]
            if removal_result["error"]:
                result["errors"].append(f"client_manager: {removal_result['error']}")
            logger.info(
                f"[stop_rtsp_stream_fallback] ClientManager cleaned: {client_id}"
            )
    except Exception as e:
        result["errors"].append(f"client_manager: {e}")
        logger.error(
            f"[stop_rtsp_stream_fallback] Failed to clean ClientManager: {client_id} - {e}"
        )

    return result
