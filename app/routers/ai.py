import cv2
import base64
import threading
import queue
import asyncio
from contextlib import asynccontextmanager
from fastapi import APIRouter, WebSocket, HTTPException, UploadFile, File, Form
from app.services.inspection import handle_network_frame

from app.services import ai,inspection
import numpy as np

# 双队列：只保留最新帧与最新推理结果
frame_queue: queue.Queue = queue.Queue(maxsize=1)
result_queue: queue.Queue = queue.Queue(maxsize=1)

# 用于停止后台线程
stop_event = threading.Event()

router = APIRouter(prefix="/ai", tags=["ai"])


@asynccontextmanager
async def lifespan():
    """AI服务生命周期管理"""
    start_background_threads()
    try:
        yield
    finally:
        stop_event.set()


@router.websocket("/video")
async def websocket_video_endpoint(websocket: WebSocket):
    """
    WebSocket端点：/ai/video
    实时视频流推理结果推送
    - 从 result_queue 获取最新推理帧
    - 以 Base64 JPEG 格式通过 WebSocket 发送
    """
    await websocket.accept()
    print(f"WebSocket 连接已建立: {websocket.client}")

    try:
        while True:
            try:
                processed_frame = result_queue.get_nowait()

                # 编码为 JPEG 并转换为 base64 字符串
                success, buffer = cv2.imencode('.jpg', processed_frame)
                if not success:
                    await asyncio.sleep(0.03)
                    continue

                jpg_as_text = base64.b64encode(buffer.tobytes()).decode('utf-8')
                data_url = f"data:image/jpeg;base64,{jpg_as_text}"

                await websocket.send_text(data_url)

            except queue.Empty:
                # 没有新结果，短暂休眠并继续循环
                await asyncio.sleep(0.03)
                continue

    except Exception as e:
        print(f"WebSocket 错误: {e}")
    finally:
        print(f"WebSocket 连接已关闭: {websocket.client}")


@router.get("/status")
async def get_ai_status():
    """获取AI服务状态"""
    return {
        "status": "running",
        "frame_queue_size": frame_queue.qsize(),
        "result_queue_size": result_queue.qsize(),
        "threads_active": len([t for t in threading.enumerate() if t.name in ["CaptureThread", "InferenceThread"]])
    }

def start_background_threads():

    inf_thread = threading.Thread(
        target=ai.inference_loop,
        args=(frame_queue, result_queue, stop_event),
        daemon=True,
        name="InferenceThread",
    )

    # cap_thread.start()
    inf_thread.start()

    print("AI后台线程已启动：推理线程（网络上传模式）")