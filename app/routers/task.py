"""
用于清洗任务控制
包括初始化、终止任务等功能
"""

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from pydantic import BaseModel
from typing import Optional
from app.services import ai, task as task_service
from app.models.task import Task as CleaningTask
import json
from datetime import datetime

router = APIRouter(prefix="/task", tags=["task"])

# 请求/响应模型
class InitializeTaskRequest(BaseModel):
    client_id: str
    actor_id: int

class StartTaskRequest(BaseModel):
    client_id: str
    task_id: int

class TerminateTaskRequest(BaseModel):
    client_id: str
    task_id: int

class TaskStatusResponse(BaseModel):
    task_id: int
    status: str
    cleaning_stage: str  # current_step现在是str
    bending_count: int
    bubble_detected: bool
    fully_submerged: bool
    updated_at: str

@router.post("/initialize", response_model=TaskStatusResponse)
async def initialize_task(request: InitializeTaskRequest):
    """初始化清洗任务"""
    try:
        # 创建新任务
        new_task = task_service.initialize_task(request.actor_id)

        # 为客户端设置任务
        success = ai.set_task(request.client_id, new_task)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to set task for client")

        return TaskStatusResponse(
            task_id=new_task.task_id,
            status=new_task.status,
            cleaning_stage=new_task.current_step,
            bending_count=new_task.bending_count,
            bubble_detected=new_task.bubble_detected,
            fully_submerged=new_task.fully_submerged,
            updated_at=datetime.fromtimestamp(new_task.updated_at).isoformat()
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to initialize task: {str(e)}")

@router.post("/start")
async def start_task(request: StartTaskRequest):
    """开始任务，设置start_time"""
    try:
        # 获取当前任务
        current_task = ai.get_task(request.client_id)
        if current_task is None or current_task.task_id != request.task_id:
            raise HTTPException(status_code=404, detail="Task not found for client")

        # 开始任务
        success = task_service.start_task(current_task)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to start task")

        return {"message": "Task started successfully", "task_id": request.task_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to start task: {str(e)}")

@router.post("/terminate")
async def terminate_task(request: TerminateTaskRequest):
    """中断/终止任务"""
    try:
        # 获取当前任务
        current_task = ai.get_task(request.client_id)
        if current_task is None or current_task.task_id != request.task_id:
            raise HTTPException(status_code=404, detail="Task not found for client")

        # 终止任务
        success = task_service.terminate_task(current_task)
        if not success:
            raise HTTPException(status_code=500, detail="Failed to terminate task")

        # 清除客户端的任务
        ai.set_task(request.client_id, None)

        return {"message": "Task terminated successfully", "task_id": request.task_id}
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to terminate task: {str(e)}")

@router.websocket("/status/{client_id}")
async def websocket_task_status(websocket: WebSocket, client_id: str):
    """实时更新任务状态的WebSocket接口"""
    await websocket.accept()
    try:
        while True:
            # 获取当前任务状态
            current_task = ai.get_task(client_id)

            if current_task:
                # 发送任务状态更新
                status_data = {
                    "task_id": current_task.task_id,
                    "status": current_task.status,
                    "cleaning_stage": current_task.current_step,
                    "bending_count": current_task.bending_count,
                    "bubble_detected": current_task.bubble_detected,
                    "fully_submerged": current_task.fully_submerged,
                    "updated_at": datetime.fromtimestamp(current_task.updated_at).isoformat()
                }
                await websocket.send_text(json.dumps(status_data))
            else:
                # 没有活跃任务
                await websocket.send_text(json.dumps({"status": "no_active_task"}))

            # 每秒更新一次状态
            import asyncio
            await asyncio.sleep(1)

    except WebSocketDisconnect:
        print(f"WebSocket 任务状态连接已关闭: {client_id}")
    except Exception as e:
        print(f"WebSocket 任务状态错误 for {client_id}: {e}")
        try:
            await websocket.close()
        except:
            pass