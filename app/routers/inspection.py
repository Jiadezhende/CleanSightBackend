from fastapi import APIRouter, WebSocket, HTTPException, Query
from pydantic import BaseModel
from app.services import ai
from app.services.stream import stream_service
import threading
import cv2
import time
import subprocess
import numpy as np
import os
from typing import Dict
import tempfile
from urllib.parse import urlparse
from pathlib import Path

router = APIRouter(prefix="/inspection", tags=["inspection"])

# 注意：旧版使用线程字典管理，这里改为由 stream_service 管理多路解码
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
 


# 旧的 _stream_capture_worker 已移至 app.services.stream_service

@router.post("/start_rtmp_stream")
async def start_rtmp_stream(config: RTMPStreamConfig):
    """
    启动 RTMP 流捕获。
    
    POST /inspection/start_rtmp_stream
    Body: {"client_id": "xxx", "rtmp_url": "rtmp://localhost:1935/live/stream", "fps": 30}
    """
    # RTMP 已弃用；保留接口签名以兼容旧客户端，但返回 410 指示迁移
    raise HTTPException(status_code=410, detail="RTMP 接口已弃用，请改用 RTSP（/inspection/start_rtsp_stream）")


@router.post("/stop_rtmp_stream")
async def stop_rtmp_stream(client_id: str = Query(..., description="客户端ID")):
    """
    停止 RTMP 流捕获。
    
    POST /inspection/stop_rtmp_stream?client_id=xxx
    """
    # RTMP 停止接口已弃用；保留路由以兼容旧客户端，但返回 410 指示迁移
    raise HTTPException(status_code=410, detail="RTMP 接口已弃用，请改用 RTSP（/inspection/stop_rtsp_stream）")


@router.post("/start_rtsp_stream")
async def start_rtsp_stream(config: RTSPStreamConfig):
    """
    启动 RTSP 流捕获。
    
    POST /inspection/start_rtsp_stream
    Body: {"client_id": "xxx", "rtsp_url": "rtsp://localhost:8554/live/stream", "fps": 30}
    """
    client_id = config.client_id
    ai.set_stream_url(client_id, config.rtsp_url)
    try:
        stream_service.start_stream(client_id=client_id, stream_url=config.rtsp_url, fps=config.fps, protocol='RTSP')
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"failed to start stream: {e}")
    return {"status": "success", "message": f"RTSP 流捕获已启动 for {client_id}"}


@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(..., description="客户端ID")):
    """
    停止 RTSP 流捕获。
    
    POST /inspection/stop_rtsp_stream?client_id=xxx
    """
    if not stream_service.has_stream(client_id):
        raise HTTPException(status_code=404, detail=f"未找到 RTSP 流 for {client_id}")
    stream_service.stop_stream(client_id)
    ai.remove_client(client_id)
    return {"status": "success", "message": f"RTSP 流捕获已停止 for {client_id}"}


@router.post("/stop_stream")
async def stop_stream(client_id: str):
    """
    通用流停止接口，同时支持 RTMP 和 RTSP。
    
    POST /inspection/stop_stream?client_id=xxx
    """
    if not stream_service.has_stream(client_id):
        raise HTTPException(status_code=404, detail=f"未找到流 for {client_id}")
    stream_service.stop_stream(client_id)
    ai.remove_client(client_id)
    return {"status": "success", "message": f"流捕获已停止 for {client_id}"}