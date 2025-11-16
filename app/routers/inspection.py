from fastapi import APIRouter, UploadFile, File, Form, HTTPException, WebSocket
from typing import List
from app.services.inspection import handle_network_frame
from app.routers.ai import frame_queue  # 导入全局队列
import asyncio

router = APIRouter(prefix="/inspection", tags=["inspection"])

@router.post("/upload_frame")
async def upload_frame(frame: str = Form(...)):
    """
    接收网络上传的视频帧（Base64编码）并加入处理队列。
    - 参数: Base64编码的图像帧
    - 返回: 状态信息
    """
    result = handle_network_frame(frame_queue, frame)

    if result == "success":
        return {"status": "success", "message": "帧已成功上传。"}
    else:
        raise HTTPException(status_code=400, detail=f"帧处理失败: {result}")


@router.websocket("/upload_stream")
async def websocket_upload_stream(websocket: WebSocket):
    """
    WebSocket端点：/inspection/upload_stream
    实时接收摄像头上传的视频帧（Base64编码）并加入处理队列。
    - 客户端持续发送帧，服务器确认接收。
    """
    await websocket.accept()
    print(f"WebSocket 上传连接已建立: {websocket.client}")

    try:
        while True:
            # 接收客户端发送的帧（Base64字符串）
            frame_data = await websocket.receive_text()
            
            # 处理帧并加入队列
            result = handle_network_frame(frame_queue, frame_data)
            
            if result == "success":
                # 发送确认消息
                await websocket.send_text("success")
            else:
                # 发送错误消息
                await websocket.send_text(f"error: {result}")
                
    except Exception as e:
        print(f"WebSocket 上传错误: {e}")
    finally:
        print(f"WebSocket 上传连接已关闭: {websocket.client}")