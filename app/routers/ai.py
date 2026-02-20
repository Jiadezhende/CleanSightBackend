import asyncio
import logging
import time
from contextlib import asynccontextmanager
from datetime import datetime

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

from app.database import get_db
from app.models.frame import ProcessedFrame
from app.models.task import DBTask, Task, TaskStatusResponse
from app.services import ai

router = APIRouter(prefix="/ai", tags=["ai"])
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan():
    """AI服务生命周期管理：启动推理管理器"""
    # 启动 AI 推理服务
    ai.start()
    logger.info("AI推理服务已启动")

    try:
        yield
    finally:
        # 停止 AI 推理服务
        ai.stop()
        logger.info("AI推理服务已停止")


@router.websocket("/video")
async def websocket_video_endpoint(websocket: WebSocket):
    """
    WebSocket端点：/ai/video?client_id=xxx
    - 要求客户端在连接时通过查询参数 `client_id` 指定自身 ID
    - 服务器持续向该 client_id 推送最新推理结果（Base64 JPEG）
    """
    # 获取 client_id
    client_id = websocket.query_params.get("client_id")
    if not client_id:
        await websocket.close(code=1008)
        return

    await websocket.accept()
    logger.info(
        f"[WebSocket] 连接已建立: client_id={client_id}, remote={websocket.client}"
    )

    # 帧率控制和去重
    last_sent_timestamp = 0.0  # 上一帧的时间戳（来自帧本身）
    last_sent_time = 0.0  # 上一次发送的系统时间
    frame_interval = 1.0 / 30  # 30fps
    frames_sent = 0
    last_log_time = time.time()

    try:
        while True:
            processed_frame: ProcessedFrame = ai.get_result(client_id, as_model=True)  # type: ignore

            if processed_frame is None:
                await asyncio.sleep(0.01)  # 减少轮询间隔
                continue

            # 去重：检查时间戳，避免重复发送同一帧
            current_timestamp = (
                processed_frame.raw_timestamp.timestamp()
                if processed_frame.raw_timestamp
                else time.time()
            )
            if current_timestamp <= last_sent_timestamp:
                await asyncio.sleep(0.01)
                continue

            # 帧率控制：确保发送间隔不小于 frame_interval
            current_time = time.time()
            if last_sent_time > 0:
                time_since_last = current_time - last_sent_time
                if time_since_last < frame_interval:
                    await asyncio.sleep(frame_interval - time_since_last)
                    current_time = time.time()  # 更新时间

            # 使用模型中的 Base64 编码图像
            try:
                data_url = (
                    f"data:image/jpeg;base64,{processed_frame.processed_frame_b64}"
                )
                await websocket.send_text(data_url)

                # 更新发送记录
                last_sent_timestamp = current_timestamp
                last_sent_time = current_time
                frames_sent += 1

                # 每5秒输出统计
                if current_time - last_log_time >= 5.0:
                    elapsed = current_time - last_log_time
                    fps = frames_sent / elapsed
                    logger.debug(
                        f"[WebSocket] client={client_id}: 发送 {frames_sent}帧/{elapsed:.1f}秒 = {fps:.1f}fps"
                    )
                    frames_sent = 0
                    last_log_time = current_time

            except WebSocketDisconnect:
                # 客户端正常断开连接
                logger.info(f"[WebSocket] 客户端正常断开: client_id={client_id}")
                break

            except (ConnectionResetError, BrokenPipeError):
                # 网络连接被重置
                logger.info(f"[WebSocket] 连接重置: client_id={client_id}")
                break

            except Exception:
                # 其他未预期的发送错误
                logger.error(
                    f"[WebSocket] 发送异常: client_id={client_id}", exc_info=True
                )
                break

    except Exception as e:
        # 捕获并记录未预期异常，便于诊断
        logger.error(f"[WebSocket] 未捕获异常: client_id={client_id}", exc_info=True)
    finally:
        # # 客户端断开时尝试清理缓存（尽量容错）
        # try:
        #     ai.remove_client(client_id)
        # except Exception:
        #     pass
        logger.info(f"[WebSocket] 连接已关闭: client_id={client_id}")


@router.get("/status")
async def get_ai_status():
    """
    获取AI服务状态，返回详细的队列信息

    ⚠️ 过渡接口：建议使用 GET /health/status 代替

    职责边界变更：
    - 系统状态查询应该由 GlobalHealthMonitor 负责
    - 此端点保留用于向后兼容，未来版本将移除
    - 新应用请使用 /health/status 获取更完整的系统状态
    """
    return ai.status()


@router.get("/load_task/{task_id}")
async def load_task(task_id: int):
    """
    加载任务，为指定 task_id 的任务在 AI 服务中创建任务对象。
    从数据库读取任务信息，使用 source_ip 作为 client_id。

    ⚠️ 过渡接口：建议使用 POST /api/start 代替

    注意：此接口只加载任务，不启动流。需要单独调用 /inspection/start_rtsp_stream
    """
    db = None
    db_task = None  # 初始化变量，避免 UnboundLocalError

    from sqlalchemy.exc import SQLAlchemyError

    from app.utils.exceptions import DatabaseError

    db = None
    try:
        db = next(get_db())
        try:
            db_task = db.query(DBTask).filter(DBTask.task_id == task_id).first()
        except SQLAlchemyError as e:
            raise DatabaseError(
                message=f"Failed to query task {task_id}",
                retryable=True,
                query=f"SELECT * FROM task WHERE task_id = {task_id}",
            ) from e

        if db_task is None:
            raise HTTPException(status_code=404, detail=f"Task {task_id} not found")

        # 使用 source_ip 作为 client_id（转换为 str 类型）
        client_id = str(db_task.source_ip)
        if not client_id or client_id == "None":
            raise HTTPException(status_code=400, detail="Task source_ip is empty")

        # 构造内存中的任务对象
        task = Task(
            task_id=task_id,
            current_step=str(db_task.current_step),
            status="running",
            updated_at=int(time.time()),
            fully_submerged=False,
            bending=False,
            bubble_detected=False,
        )

        logger.debug(f"[load_task] 为 client_id={client_id} 加载任务 {task.task_id}")

        # 为客户端设置任务
        success = ai.set_task(client_id, task)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to set task for client")

        return TaskStatusResponse(
            task_id=task.task_id,
            status=task.status,
            cleaning_stage=task.current_step,
            bending=task.bending,
            bubble_detected=task.bubble_detected,
            fully_submerged=task.fully_submerged,
            updated_at=datetime.fromtimestamp(task.updated_at).isoformat(),
        )
    finally:
        if db:
            db.close()


@router.post("/terminate_task/{client_id}")
async def terminate_task(client_id: str):
    """
    终止任务，清理指定 client_id 的推理资源。

    ⚠️ 过渡接口：建议使用 POST /api/terminate 代替

    注意：此接口只清理推理资源（落盘数据），不停止流解码器，不清理 ClientManager
    完整清理请使用 POST /api/terminate

    Args:
        client_id: 客户端 ID（通常是 source_ip）
    """
    # 只清理推理资源（落盘残余数据）
    ai.remove_client(client_id)
    return {"status": "success", "message": f"Task terminated for client {client_id}"}


def start_background_threads():
    # 启动由 ai 管理器负责的推理线程
    ai.start()
    print("AI后台线程已启动（多客户端推理管理器）")
