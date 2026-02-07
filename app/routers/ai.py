from datetime import datetime
import asyncio
from contextlib import asynccontextmanager
import time
from app.database import get_db
from app.models.task import DBTask, Task, TaskStatusResponse
from fastapi import APIRouter, HTTPException, WebSocket
import traceback
from app.services import ai
from app.models.frame import ProcessedFrame

router = APIRouter(prefix="/ai", tags=["ai"])


@asynccontextmanager
async def lifespan():
    """AI服务生命周期管理：启动/停止推理管理器"""
    ai.start()
    try:
        yield
    finally:
        ai.stop()


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
    print(f"WebSocket 连接已建立 (client_id={client_id}): {websocket.client}")

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
            current_timestamp = processed_frame.raw_timestamp.timestamp() if processed_frame.raw_timestamp else time.time()
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
                data_url = f"data:image/jpeg;base64,{processed_frame.processed_frame_b64}"
                await websocket.send_text(data_url)
                
                # 更新发送记录
                last_sent_timestamp = current_timestamp
                last_sent_time = current_time
                frames_sent += 1
                
                # 每5秒输出统计
                if current_time - last_log_time >= 5.0:
                    elapsed = current_time - last_log_time
                    fps = frames_sent / elapsed
                    print(f"[WebSocket] client={client_id}: 发送 {frames_sent}帧/{elapsed:.1f}秒 = {fps:.1f}fps")
                    frames_sent = 0
                    last_log_time = current_time
            except Exception as send_exc:
                # 避免在 except 子句中直接引用可能未导入的异常类名，改为运行时检查异常类型名或常见连接错误
                exc_name = send_exc.__class__.__name__
                if exc_name in ("WebSocketDisconnect", "ClientDisconnected", "ConnectionClosedOK") or isinstance(send_exc, (ConnectionResetError, BrokenPipeError)):
                    print(f"WebSocket 客户端断开或连接已关闭 (client_id={client_id}): {exc_name}")
                    break
                else:
                    print(f"WebSocket 发送异常 (client_id={client_id}): {send_exc}")
                    traceback.print_exc()
                    break

    except Exception as e:
        # 捕获并打印未预期异常，便于诊断
        print(f"WebSocket 未捕获异常 (client_id={client_id}): {e}")
        traceback.print_exc()
    finally:
        # # 客户端断开时尝试清理缓存（尽量容错）
        # try:
        #     ai.remove_client(client_id)
        # except Exception:
        #     pass
        client_info = getattr(websocket, 'client', None)
        print(f"WebSocket 连接已关闭 (client_id={client_id}): {client_info}")

@router.get("/status")
async def get_ai_status():
    """获取AI服务状态，返回详细的队列信息"""
    return ai.status()

@router.get("/load_task/{task_id}")
async def load_task(task_id: int):
    """
    加载任务，为指定 task_id 的任务在 AI 服务中创建任务对象。
    从数据库读取任务信息，使用 source_ip 作为 client_id。
    """
    db = None
    db_task = None  # 初始化变量，避免 UnboundLocalError
    
    from app.utils.exceptions import DatabaseError
    from sqlalchemy.exc import SQLAlchemyError

    db = None
    try:
        db = next(get_db())
        try:
            db_task = db.query(DBTask).filter(DBTask.task_id == task_id).first()
        except SQLAlchemyError as e:
            raise DatabaseError(
                message=f"Failed to query task {task_id}",
                retryable=True,
                query=f"SELECT * FROM task WHERE task_id = {task_id}"
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
            bubble_detected=False
        )

        print(f"为 client_id={client_id} 加载任务 {task}")

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
            updated_at=datetime.fromtimestamp(task.updated_at).isoformat()
        )
    finally:
        if db:
            db.close()
    
@router.post("/terminate_task/{client_id}")
async def terminate_task(client_id: str):
    """
    终止任务，清理指定 client_id 的所有 AI 服务资源（队列、任务对象等）。

    业务代码（纯净）：让异常向上传播到边界层 3（FastAPI全局处理器）

    Args:
        client_id: 客户端 ID（通常是 source_ip）
    """
    # 清理 AI 服务中的客户端资源
    # 如果内部有清理失败，会抛出具体的异常（由边界层 3 处理）
    ai.remove_client(client_id)
    return {"status": "success", "message": f"Task terminated for client {client_id}"}


def start_background_threads():
    # 启动由 ai 管理器负责的推理线程
    ai.start()
    print("AI后台线程已启动（多客户端推理管理器）")