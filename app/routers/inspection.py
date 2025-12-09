from fastapi import APIRouter, WebSocket, HTTPException, Query
from pydantic import BaseModel
from app.services import ai
import threading
import cv2
import time
import subprocess
import numpy as np
import os
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


class RTSPStreamConfig(BaseModel):
    """RTSP 流配置"""
    client_id: str
    rtsp_url: str
    fps: int = 30  # 固定帧率


def _stream_capture_worker(client_id: str, stream_url: str, fps: int, stop_event: threading.Event, protocol: str = "RTMP"):
    """通用流捕获工作线程，支持 RTMP 和 RTSP 协议。"""
    print(f"[{protocol} Worker] 启动捕获线程 for {client_id}: {stream_url}")
    
    # 查找 ffmpeg 可执行文件
    def _find_ffmpeg():
        # 优先尝试系统 PATH
        try:
            result = subprocess.run(['ffmpeg', '-version'], 
                                  capture_output=True, 
                                  timeout=2)
            if result.returncode == 0:
                return 'ffmpeg'
        except:
            pass
        
        # 备用：Chocolatey 安装的版本
        choco_path = r"C:\ProgramData\chocolatey\lib\ffmpeg\tools\ffmpeg\bin\ffmpeg.exe"
        if os.path.exists(choco_path):
            return choco_path
        
        return None
    
    ffmpeg_path = _find_ffmpeg()
    if not ffmpeg_path:
        print(f"[{protocol} Worker] ❌ 未找到 ffmpeg，无法捕获 {protocol} 流")
        return
    
    # 构建 ffmpeg 命令 - 支持 RTMP 和 RTSP 协议
    cmd = [
        ffmpeg_path,
        '-rtsp_transport', 'tcp',  # 确保 RTSP 使用 TCP 传输
        '-i', stream_url,
        '-f', 'rawvideo',
        '-pix_fmt', 'bgr24',
        '-vf', f'fps={fps},scale=640:480',
        '-'
    ]

    frame_count = 0
    frame_size = 640 * 480 * 3
    
    print(f"[{protocol} Worker] 启动 ffmpeg: {' '.join(cmd[:3])}...")
    
    try:
        process = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,  # 捕获错误信息
            bufsize=0
        )
        
        print(f"[{protocol} Worker] ffmpeg 进程已启动 (PID: {process.pid})")
        
        # 短暂等待看是否有立即的错误
        time.sleep(2)
        if process.poll() is not None:
            stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
            print(f"[{protocol} Worker] ffmpeg 进程提前退出 (退出码: {process.returncode})")
            print(f"[{protocol} Worker] 错误信息: {stderr_output}")
            return
        
        buffer = b''
        while not stop_event.is_set() and process.poll() is None:
            try:
                # 读取数据块
                chunk = process.stdout.read(32768)  # 32KB 缓冲区
                if len(chunk) == 0:
                    print(f"[{protocol} Worker] 接收到 0 字节数据，流结束")
                    break
                
                buffer += chunk
                
                # 检查是否有完整帧
                while len(buffer) >= frame_size:
                    frame_data = buffer[:frame_size]
                    buffer = buffer[frame_size:]
                    
                    frame = np.frombuffer(frame_data, dtype=np.uint8).reshape((480, 640, 3))
                    ai.submit_frame(client_id, frame)
                    frame_count += 1
                    
                    if frame_count % 30 == 0:  # 每秒报告一次 (假设30fps)
                        print(f"[{protocol} Worker] 已处理 {frame_count} 帧")
                        
            except Exception as e:
                print(f"[{protocol} Worker] 处理帧时出错: {e}")
                break
        
    except Exception as e:
        print(f"[{protocol} Worker] 异常: {e}")
        import traceback
        traceback.print_exc()
    finally:
        if 'process' in locals():
            try:
                if process.poll() is None:
                    process.terminate()
                    process.wait(timeout=3)
                else:
                    # 进程已退出，获取错误信息
                    if hasattr(process, 'stderr') and process.stderr:
                        try:
                            stderr_output = process.stderr.read().decode('utf-8', errors='ignore')
                            if stderr_output:
                                print(f"[{protocol} Worker] 进程错误信息: {stderr_output}")
                        except:
                            pass
            except:
                try:
                    process.kill()
                except:
                    pass
        
        print(f"[{protocol} Worker] 停止，共处理 {frame_count} 帧")


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
        target=_stream_capture_worker,
        args=(client_id, config.rtmp_url, config.fps, stop_event, "RTMP"),
        daemon=True,
        name=f"RTMPCapture-{client_id}"
    )
    _capture_threads[client_id] = thread
    thread.start()
    
    return {"status": "success", "message": f"RTMP 流捕获已启动 for {client_id}"}


@router.post("/stop_rtmp_stream")
async def stop_rtmp_stream(client_id: str = Query(..., description="客户端ID")):
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


@router.post("/start_rtsp_stream")
async def start_rtsp_stream(config: RTSPStreamConfig):
    """
    启动 RTSP 流捕获。
    
    POST /inspection/start_rtsp_stream
    Body: {"client_id": "xxx", "rtsp_url": "rtsp://localhost:8554/live/stream", "fps": 30}
    """
    client_id = config.client_id
    
    # 检查是否已经在运行
    if client_id in _capture_threads and _capture_threads[client_id].is_alive():
        raise HTTPException(status_code=400, detail=f"RTSP 流已在运行 for {client_id}")
    
    # 设置 RTSP URL
    ai.set_rtmp_url(client_id, config.rtsp_url)  # 重用现有的 AI 服务接口
    
    # 创建停止事件
    stop_event = threading.Event()
    _stop_events[client_id] = stop_event
    
    # 启动捕获线程
    thread = threading.Thread(
        target=_stream_capture_worker,
        args=(client_id, config.rtsp_url, config.fps, stop_event, "RTSP"),
        daemon=True,
        name=f"RTSPCapture-{client_id}"
    )
    _capture_threads[client_id] = thread
    thread.start()
    
    return {"status": "success", "message": f"RTSP 流捕获已启动 for {client_id}"}


@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(..., description="客户端ID")):
    """
    停止 RTSP 流捕获。
    
    POST /inspection/stop_rtsp_stream?client_id=xxx
    """
    if client_id not in _capture_threads:
        raise HTTPException(status_code=404, detail=f"未找到 RTSP 流 for {client_id}")
    
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
    
    return {"status": "success", "message": f"RTSP 流捕获已停止 for {client_id}"}


@router.post("/stop_stream")
async def stop_stream(client_id: str):
    """
    通用流停止接口，同时支持 RTMP 和 RTSP。
    
    POST /inspection/stop_stream?client_id=xxx
    """
    if client_id not in _capture_threads:
        raise HTTPException(status_code=404, detail=f"未找到流 for {client_id}")
    
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
    
    return {"status": "success", "message": f"流捕获已停止 for {client_id}"}