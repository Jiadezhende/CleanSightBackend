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

import asyncio
import logging
from typing import Dict

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.domain.task import CleaningTask
from app.models import DBTask
from app.routers.health import get_health_monitor
from app.services.client.manager import client_manager
from app.services.inference.instance import inference_manager
from app.services.run_control import run_controller
from app.services.stream import stream_service
from app.utils.exceptions import AppError, DatabaseError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["unified"])

# per-client 锁：保证同一 client_id 的 start/terminate 操作串行执行
_client_locks: Dict[str, asyncio.Lock] = {}
_client_locks_guard = asyncio.Lock()


async def _get_client_lock(client_id: str) -> asyncio.Lock:
    """获取指定 client_id 的异步锁（懒创建）"""
    async with _client_locks_guard:
        if client_id not in _client_locks:
            _client_locks[client_id] = asyncio.Lock()
        return _client_locks[client_id]


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
    1. 从数据库加载任务（锁外执行，无并发风险）
    2. 加锁后：幂等检查 / 跨任务切换清理 / 设置新任务 / 启动流

    Args:
        req: 包含 task_id, rtsp_url, fps 的启动请求

    Returns:
        启动结果，包含 client_id, task_id, rtsp_url

    Raises:
        HTTPException: 任务不存在、source_ip为空、启动失败等
    """
    db = None
    try:
        # ── Phase 1: DB 查询 + 校验（锁外，无并发风险）──
        db = next(get_db())
        try:
            db_task = db.query(DBTask).filter(DBTask.task_id == req.task_id).first()
        except SQLAlchemyError as e:
            raise DatabaseError(
                message=f"Failed to query task {req.task_id}",
                retryable=True,
                query="task_lookup_by_id",
            ) from e

        if not db_task:
            raise NotFoundError(
                message=f"Task {req.task_id} not found",
                resource_type="Task",
                resource_id=str(req.task_id),
            )

        source_ip: str | None = db_task.source_ip  # type: ignore[assignment]
        if not source_ip:
            raise ValidationError(
                message="Task source_ip is required", field="source_ip", value=None
            )

        client_id = source_ip
        logger.info(f"[start] Starting task {req.task_id} for client {client_id}")

        # ── Phase 2: 加锁，保护 client_id 相关的所有状态变更 ──
        lock = await _get_client_lock(client_id)
        async with lock:
            # 2a. 幂等检查 + 清理已有客户端
            if client_manager.has_client(client_id):
                cq = client_manager.get(client_id)
                old_task = cq.get_task()
                old_task_id = cq.get_task_id()

                # 完全相同（task_id、step、URL 均未变）才幂等返回，否则一律全量重建
                if (
                    old_task_id == req.task_id
                    and old_task is not None
                    and old_task.current_step == str(db_task.current_step)
                    and (stream_service.get_stream_info(client_id) or {}).get("url") == req.rtsp_url
                ):
                    logger.info(
                        f"[start] Task {req.task_id} already running for {client_id}, "
                        f"idempotent return"
                    )
                    return {
                        "status": "success",
                        "client_id": client_id,
                        "task_id": req.task_id,
                        "rtsp_url": req.rtsp_url,
                        "message": f"Task {req.task_id} already running (idempotent)",
                    }

                # 任何字段变化（task_id / step / URL）→ 停止旧客户端，全量重建
                logger.info(
                    f"[start] Client {client_id} exists (task={old_task_id}), "
                    f"performing full cleanup before restart"
                )
                monitor = get_health_monitor()
                if monitor:
                    cleanup_result = monitor.cleanup_client(
                        client_id=client_id,
                        reason=f"restart:{old_task_id}->{req.task_id}",
                    )
                else:
                    cleanup_result = run_controller.stop_run(
                        client_id, reason=f"restart:{old_task_id}->{req.task_id}"
                    )

                if cleanup_result.get("errors"):
                    logger.warning(
                        f"[start] Cleanup before restart had errors: "
                        f"{cleanup_result['errors']}"
                    )

            # 2b. 设置新任务
            task = CleaningTask(
                task_id=req.task_id,
                current_step=str(db_task.current_step),
                status="running",
            )

            success = inference_manager.set_task(client_id, task)
            if not success:
                raise AppError(
                    message=f"Failed to set task for client {client_id}",
                    client_id=client_id,
                )

            logger.info(
                f"[start] Task set successfully: {client_id} -> task_id={req.task_id}"
            )

            # 2c. 启动流
            stream_service.start_stream(
                client_id=client_id,
                stream_url=req.rtsp_url,
                fps=req.fps,
                protocol="RTSP",
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
        - 与 start() 共用 per-client 锁，防止并发竞态
    """
    lock = await _get_client_lock(client_id)
    async with lock:
        logger.info(f"[terminate] Terminating client: {client_id}")

        # 路由清理到健康监控（统一入口）
        monitor = get_health_monitor()
        if monitor:
            result = monitor.cleanup_client(
                client_id=client_id, reason="API termination request"
            )
        else:
            # Fallback: 健康监控未初始化，直接走 RunController（拆除唯一实现）
            logger.warning(
                "[terminate] Health monitor not initialized, calling RunController directly"
            )
            result = run_controller.stop_run(
                client_id, reason="API termination request"
            )

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
