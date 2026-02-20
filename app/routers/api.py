"""
统一 API 路由

提供简化的启动和终止接口：
- POST /api/start: 合并 load_task + start_rtsp_stream
- POST /api/terminate: 统一的终止接口

职责清晰：
- 检测跨任务切换并清理旧数据
- 协调 InferenceManager 和 StreamService
- 负责 ClientManager 的清理
"""

import logging
import time

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models.task import DBTask, Task
from app.routers.health import get_health_monitor
from app.services import ai
from app.services.client.manager import client_manager
from app.services.stream import stream_service
from app.utils.exceptions import AppError, DatabaseError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["unified"])


class StartRequest(BaseModel):
    """启动请求"""

    task_id: int
    rtsp_url: str
    fps: int = 30


@router.post("/start")
async def start(req: StartRequest):
    """
    统一的启动接口（合并 load_task + start_rtsp_stream）

    流程：
    1. 从数据库加载任务
    2. 检测跨任务切换，清理旧数据
    3. 设置新任务
    4. 启动流

    Args:
        req: 包含 task_id, rtsp_url, fps 的启动请求

    Returns:
        启动结果，包含 client_id, task_id, rtsp_url

    Raises:
        HTTPException: 任务不存在、source_ip为空、启动失败等
    """
    db = None
    try:
        # 1. 从数据库加载任务
        db = next(get_db())
        try:
            db_task = db.query(DBTask).filter(DBTask.task_id == req.task_id).first()
        except SQLAlchemyError as e:
            raise DatabaseError(
                message=f"Failed to query task {req.task_id}",
                retryable=True,
                query=f"SELECT * FROM task WHERE task_id = {req.task_id}",
            ) from e

        if not db_task:
            raise NotFoundError(
                message=f"Task {req.task_id} not found",
                resource_type="Task",
                resource_id=str(req.task_id),
            )

        if not db_task.source_ip:
            raise ValidationError(
                message="Task source_ip is required", field="source_ip", value=None
            )

        client_id = str(db_task.source_ip)
        logger.info(f"[start] Starting task {req.task_id} for client {client_id}")

        # 2. 检测跨任务切换，清理旧数据
        if client_manager.has_client(client_id):
            cq = client_manager.get_client(client_id)
            old_task_id = cq.get_task_id()

            if old_task_id is not None and old_task_id != req.task_id:
                # 跨任务切换：清理旧数据
                logger.info(
                    f"[start] Task changed for {client_id}: {old_task_id} → {req.task_id}, clearing old data"
                )
                removal_result = client_manager.remove_client(client_id, cleanup=True)
                if removal_result["error"]:
                    logger.warning(
                        f"[start] Failed to clean old client data: {removal_result['error']}"
                    )

        # 3. 设置新任务
        task = Task(
            task_id=req.task_id,
            current_step=str(db_task.current_step),
            status="running",
            updated_at=int(time.time()),
            fully_submerged=False,
            bending=False,
            bubble_detected=False,
        )

        success = ai.set_task(client_id, task)
        if not success:
            raise AppError(
                message=f"Failed to set task for client {client_id}",
                client_id=client_id,
            )

        logger.info(
            f"[start] Task set successfully: {client_id} -> task_id={req.task_id}"
        )

        # 4. 启动流
        ai.set_stream_url(client_id, req.rtsp_url)
        stream_service.start_stream(
            client_id=client_id, stream_url=req.rtsp_url, fps=req.fps, protocol="RTSP"
        )

        logger.info(f"[start] Stream started successfully: {client_id}")

        return {
            "status": "success",
            "client_id": client_id,
            "task_id": req.task_id,
            "rtsp_url": req.rtsp_url,
            "message": f"Task {req.task_id} started for client {client_id}",
        }

    finally:
        if db:
            db.close()


@router.post("/terminate")
async def terminate(client_id: str):
    """
    统一的终止接口

    职责边界：
    - API 层负责接收用户请求
    - 清理协调由 GlobalHealthMonitor 负责
    - 通过 cleanup_client() 执行统一的 3 步清理

    流程：
    1. 停止流（解码器）
    2. 落盘残余数据（通过 InferenceManager）
    3. 清理 ClientManager

    Args:
        client_id: 客户端ID

    Returns:
        清理结果，包含各步骤的成功状态和错误信息

    注意：
        - 采用"尽力而为"策略，即使某步骤失败也会继续执行后续步骤
        - 永不抛出异常，总是返回结果
    """
    logger.info(f"[terminate] Terminating client: {client_id}")

    # 路由清理到健康监控（统一入口）
    monitor = get_health_monitor()
    if monitor:
        result = monitor.cleanup_client(
            client_id=client_id, reason="API termination request"
        )
    else:
        # Fallback: 健康监控未初始化，执行手动清理
        logger.warning(
            "[terminate] Health monitor not initialized, using fallback cleanup"
        )
        result = _manual_cleanup_fallback(client_id)

    # 调整返回格式以匹配原有 API
    if result["errors"]:
        result["status"] = "partial_success"
        logger.warning(
            f"[terminate] Completed with errors: {client_id} - {result['errors']}"
        )
    else:
        result["status"] = "success"
        logger.info(f"[terminate] Terminated successfully: {client_id}")

    return result


def _manual_cleanup_fallback(client_id: str):
    """Fallback cleanup when health monitor not available

    职责边界：
    - 仅在健康监控未初始化时使用
    - 执行与 cleanup_client() 相同的 3 步清理
    - 生产环境不应触发此路径（说明初始化有问题）
    """
    result = {
        "client_id": client_id,
        "reason": "API termination (fallback)",
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
            logger.info(f"[terminate_fallback] Decoder stopped: {client_id}")
    except Exception as e:
        result["errors"].append(f"decoder: {e}")
        logger.error(f"[terminate_fallback] Failed to stop decoder: {client_id} - {e}")

    # 2. 落盘残余数据
    try:
        ai.remove_client(client_id)
        result["data_flushed"] = True
        logger.info(f"[terminate_fallback] Data flushed: {client_id}")
    except Exception as e:
        result["errors"].append(f"flush: {e}")
        logger.error(f"[terminate_fallback] Failed to flush data: {client_id} - {e}")

    # 3. 清理 ClientManager
    try:
        if client_manager.has_client(client_id):
            removal_result = client_manager.remove_client(client_id, cleanup=True)
            result["client_cleaned"] = removal_result["removed"]
            if removal_result["error"]:
                result["errors"].append(f"client_manager: {removal_result['error']}")
            logger.info(f"[terminate_fallback] ClientManager cleaned: {client_id}")
    except Exception as e:
        result["errors"].append(f"client_manager: {e}")
        logger.error(
            f"[terminate_fallback] Failed to clean ClientManager: {client_id} - {e}"
        )

    return result
