from datetime import datetime
import asyncio
from contextlib import asynccontextmanager
import time
from app.database import get_db
from app.models.task import DBTask, Task, TaskStatusResponse
from fastapi import APIRouter, HTTPException, WebSocket
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

    try:
        while True:
            processed_frame :ProcessedFrame= ai.get_result(client_id, as_model=True) # type: ignore

            if processed_frame is None:
                await asyncio.sleep(0.03)
                continue

            # 使用模型中的 Base64 编码图像
            data_url = f"data:image/jpeg;base64,{processed_frame.processed_frame_b64}"
            await websocket.send_text(data_url)

    except Exception as e:
        print(f"WebSocket 错误 (client_id={client_id}): {e}")
    finally:
        # 客户端断开时清理缓存（可选）
        ai.remove_client(client_id)
        print(f"WebSocket 连接已关闭 (client_id={client_id}): {websocket.client}")

@router.get("/status")
async def get_ai_status():
    """获取AI服务状态，返回详细的队列信息"""
    return ai.status()

@router.get("/load_task/{task_id}")
async def start_task(task_id: int):
    """
    开始任务，为指定 task_id 的任务在 AI 服务中创建任务对象。
    """
    try:
        # 获取当前任务
        if task_id is None:
            return False

        db = next(get_db())
        db_task = db.query(DBTask).filter(DBTask.task_id == task_id).first()
        if db_task is None:
            return False
        
        # 构造内存中的任务对象
        task = Task(
            task_id=task_id,
            current_step=db_task.current_step, # type: ignore
            status="running",
            updated_at=int(time.time()),
            fully_submerged=False,
            bending=False,
            bubble_detected=False
        )

        # 为客户端设置任务
        success = ai.set_task(db_task.source_ip, task) # type: ignore
        if not success:
            raise HTTPException(status_code=500, detail="Failed to set task for client")

        return TaskStatusResponse(
            task_id=task.task_id,
            status=task.status,
            cleaning_stage=task.current_step,
            bending_count=task.bending,
            bubble_detected=task.bubble_detected,
            fully_submerged=task.fully_submerged,
            updated_at=datetime.fromtimestamp(task.updated_at).isoformat()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize task: {str(e)}")
    
@router.post("/terminate_task/{client_id}")
async def terminate_task(client_id: str):
    """
    终止任务，为指定 task_id 的任务在 AI 服务中标记为已终止。
    """
    try:
        success = ai.terminate_task_by_id(client_id)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to terminate task")
        return {"status": "success", "message": f"Task terminated"}
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to terminate task: {str(e)}")


def start_background_threads():
    # 启动由 ai 管理器负责的推理线程
    ai.start()
    print("AI后台线程已启动（多客户端推理管理器）")