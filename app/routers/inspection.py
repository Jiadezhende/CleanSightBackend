from fastapi import APIRouter, WebSocket, HTTPException
from pydantic import BaseModel
from app.services import ai
import threading
import cv2
import time
from typing import Dict

router = APIRouter(prefix="/inspection", tags=["inspection"])

# 存储正在运行的 RTMP 捕获线程
_capture_threads: Dict[str, threading.Thread] = {}
_stop_events: Dict[str, threading.Event] = {}


class RTMPStreamConfig(BaseModel):
    """RTMP 流配置"""
    client_id: str
    rtmp_url: str
    fps: int = 30  # 固定帧率


def _rtmp_capture_worker(client_id: str, rtmp_url: str, fps: int, stop_event: threading.Event):
    """RTMP 流捕获工作线程，以固定帧率提取帧。"""
    print(f"启动 RTMP 捕获线程 for {client_id}: {rtmp_url}")
    
    cap = cv2.VideoCapture(rtmp_url)
    if not cap.isOpened():
        print(f"无法打开 RTMP 流: {rtmp_url}")
        return
    
    frame_interval = 1.0 / fps  # 帧间隔（秒）
    last_capture_time = 0.0
    
    try:
        while not stop_event.is_set():
            current_time = time.time()
            
            # 检查是否到达下一帧的时间
            if current_time - last_capture_time >= frame_interval:
                ret, frame = cap.read()
                if not ret:
                    print(f"RTMP 流读取失败 for {client_id}")
                    time.sleep(0.1)
                    continue
                
                # 提交到 CA-RawQueue
                ai.submit_frame(client_id, frame)
                last_capture_time = current_time
            else:
                # 短暂休眠，避免 CPU 空转
                time.sleep(0.001)
                
    except Exception as e:
        print(f"RTMP 捕获异常 for {client_id}: {e}")
    finally:
        cap.release()
        print(f"RTMP 捕获线程已停止 for {client_id}")


@router.post("/start_rtmp_stream")
async def start_rtmp_stream(config: RTMPStreamConfig):
    """
    启动 RTMP 流捕获。
    
    POST /inspection/start_rtmp_stream
    Body: {"client_id": "xxx", "rtmp_url": "rtmp://localhost:1935/live/stream", "fps": 30}
    """
    client_id = config.client_id
    
    # 检查是否已经在运行
    if client_id in _capture_threads and _capture_threads[client_id].is_alive():
        raise HTTPException(status_code=400, detail=f"RTMP 流已在运行 for {client_id}")
    
    # 设置 RTMP URL
    ai.set_rtmp_url(client_id, config.rtmp_url)
    
    # 创建停止事件
    stop_event = threading.Event()
    _stop_events[client_id] = stop_event
    
    # 启动捕获线程
    thread = threading.Thread(
        target=_rtmp_capture_worker,
        args=(client_id, config.rtmp_url, config.fps, stop_event),
        daemon=True,
        name=f"RTMPCapture-{client_id}"
    )
    _capture_threads[client_id] = thread
    thread.start()
    
    return {"status": "success", "message": f"RTMP 流捕获已启动 for {client_id}"}


@router.post("/stop_rtmp_stream")
async def stop_rtmp_stream(client_id: str):
    """
    停止 RTMP 流捕获。
    
    POST /inspection/stop_rtmp_stream?client_id=xxx
    """
    if client_id not in _capture_threads:
        raise HTTPException(status_code=404, detail=f"未找到 RTMP 流 for {client_id}")
    
    # 发送停止信号
    if client_id in _stop_events:
        _stop_events[client_id].set()
    
    # 等待线程结束
    thread = _capture_threads[client_id]
    thread.join(timeout=2.0)
    
    # 清理
    _capture_threads.pop(client_id, None)
    _stop_events.pop(client_id, None)
    ai.remove_client(client_id)
    
    return {"status": "success", "message": f"RTMP 流捕获已停止 for {client_id}"}