"""
统一 API 路由

- POST /api/start: DB 加载任务 → 委托 RunController.start_run
- POST /api/terminate: 委托 RunController.stop_run

编排（幂等 / 重启清理 / set_task / 起流 / 拆除 + 生命周期锁）全部收敛在 RunController；
本层只做 HTTP/DB 边界 + `asyncio.to_thread` 桥接（把同步的持锁段挪出事件循环）。
"""

import asyncio
import logging

from fastapi import APIRouter
from pydantic import BaseModel
from sqlalchemy.exc import SQLAlchemyError

from app.database import get_db
from app.models import DBTask
from app.services.run_control import run_controller
from app.utils.exceptions import DatabaseError, NotFoundError, ValidationError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["unified"])


class StartRequest(BaseModel):
    """启动请求"""

    task_id: int
    rtsp_url: str
    fps: int = 30


@router.post("/start")
async def start(req: StartRequest):
    """统一启动：DB 加载任务 → 委托 `RunController.start_run`（含幂等/重启/set_task/起流）。

    Raises:
        经异常 handler 转 HTTP：任务不存在 / source_ip 为空 / DB 失败 / 启动失败等。
    """
    db = None
    try:
        # DB 查询 + 校验（HTTP/DB 边界，锁外）
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
        current_step = str(db_task.current_step)
        logger.info(f"[start] Starting task {req.task_id} for client {client_id}")

        # 编排 + 生命周期锁在 RunController；同步持锁段丢进线程，避免阻塞事件循环
        return await asyncio.to_thread(
            run_controller.start_run,
            client_id,
            req.task_id,
            current_step,
            req.rtsp_url,
            req.fps,
        )
    finally:
        if db:
            db.close()


@router.post("/terminate")
async def terminate(client_id: str):
    """统一终止：委托 `RunController.stop_run`（尽力而为，永不抛出）。"""
    logger.info(f"[terminate] Terminating client: {client_id}")

    result = await asyncio.to_thread(
        run_controller.stop_run, client_id, "API termination request"
    )

    if result["errors"]:
        result["status"] = "partial_success"
        logger.warning(
            f"[terminate] Completed with errors: {client_id} - {result['errors']}"
        )
    else:
        result["status"] = "success"
        logger.info(f"[terminate] Terminated successfully: {client_id}")

    return result
