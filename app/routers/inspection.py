from fastapi import APIRouter, WebSocket, HTTPException, Query
from pydantic import BaseModel
from app.services import ai
from app.services.decoder import get_decoder_pool
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

@router.post("/start_rtmp_stream")
async def start_rtmp_stream(config: RTMPStreamConfig):
    """
    启动 RTMP 流捕获（使用进程池）。
    
    POST /inspection/start_rtmp_stream
    Body: {"client_id": "xxx", "rtmp_url": "rtmp://localhost:1935/live/stream", "fps": 30}
    """
    client_id = config.client_id
    
    # 获取解码器进程池（已在启动时初始化）
    decoder_pool = get_decoder_pool()
    
    # 检查是否已存在
    stats = decoder_pool.get_stats()
    if client_id in stats["tasks"]:
        raise HTTPException(status_code=400, detail=f"RTMP 流已在运行 for {client_id}")
    
    # 设置流地址
    ai.set_stream_url(client_id, config.rtmp_url)
    
    # 启动解码器进程
    success = decoder_pool.start_decoder(
        client_id=client_id,
        stream_url=config.rtmp_url,
        protocol="RTMP",
        fps=config.fps
    )
    
    if not success:
        raise HTTPException(status_code=500, detail=f"启动解码器失败 for {client_id}")
    
    return {
        "status": "success", 
        "message": f"RTMP 流捕获已启动 for {client_id} (进程池模式)",
        "pool_stats": decoder_pool.get_stats()
    }


@router.post("/stop_rtmp_stream")
async def stop_rtmp_stream(client_id: str = Query(..., description="客户端ID")):
    """
    停止 RTMP 流捕获（进程池模式）。
    
    POST /inspection/stop_rtmp_stream?client_id=xxx
    """
    decoder_pool = get_decoder_pool()
    
    # 检查是否存在
    stats = decoder_pool.get_stats()
    if client_id not in stats["tasks"]:
        raise HTTPException(status_code=404, detail=f"未找到 RTMP 流 for {client_id}")
    
    # 停止解码器进程
    success = decoder_pool.stop_decoder(client_id)
    
    # 清理AI服务
    ai.remove_client(client_id)
    
    return {
        "status": "success", 
        "message": f"RTMP 流捕获已停止 for {client_id}",
        "pool_stats": decoder_pool.get_stats()
    }


@router.post("/start_rtsp_stream")
async def start_rtsp_stream(config: RTSPStreamConfig):
    """
    启动 RTSP 流捕获（使用进程池）。
    
    POST /inspection/start_rtsp_stream
    Body: {"client_id": "xxx", "rtsp_url": "rtsp://localhost:8554/live/stream", "fps": 30}
    """
    client_id = config.client_id
    
    # 获取解码器进程池（已在启动时初始化）
    decoder_pool = get_decoder_pool()
    
    # 检查是否已存在
    stats = decoder_pool.get_stats()
    if client_id in stats["tasks"]:
        raise HTTPException(status_code=400, detail=f"RTSP 流已在运行 for {client_id}")
    
    # 设置流地址
    ai.set_stream_url(client_id, config.rtsp_url)
    
    # 启动解码器进程
    success = decoder_pool.start_decoder(
        client_id=client_id,
        stream_url=config.rtsp_url,
        protocol="RTSP",
        fps=config.fps
    )
    
    if not success:
        raise HTTPException(status_code=500, detail=f"启动解码器失败 for {client_id}")
    
    return {
        "status": "success", 
        "message": f"RTSP 流捕获已启动 for {client_id} (进程池模式)",
        "pool_stats": decoder_pool.get_stats()
    }


@router.post("/stop_rtsp_stream")
async def stop_rtsp_stream(client_id: str = Query(..., description="客户端ID")):
    """
    停止 RTSP 流捕获（进程池模式）。
    
    POST /inspection/stop_rtsp_stream?client_id=xxx
    """
    decoder_pool = get_decoder_pool()
    
    # 检查是否存在
    stats = decoder_pool.get_stats()
    if client_id not in stats["tasks"]:
        raise HTTPException(status_code=404, detail=f"未找到 RTSP 流 for {client_id}")
    
    # 停止解码器进程
    success = decoder_pool.stop_decoder(client_id)
    
    # 清理AI服务
    ai.remove_client(client_id)
    
    return {
        "status": "success", 
        "message": f"RTSP 流捕获已停止 for {client_id}",
        "pool_stats": decoder_pool.get_stats()
    }


@router.post("/stop_stream")
async def stop_stream(client_id: str):
    """
    通用流停止接口，同时支持 RTMP 和 RTSP（进程池模式）。
    
    POST /inspection/stop_stream?client_id=xxx
    """
    decoder_pool = get_decoder_pool()
    
    # 检查是否存在
    stats = decoder_pool.get_stats()
    if client_id not in stats["tasks"]:
        raise HTTPException(status_code=404, detail=f"未找到流 for {client_id}")
    
    # 停止解码器进程
    success = decoder_pool.stop_decoder(client_id)
    
    # 清理AI服务
    ai.remove_client(client_id)
    
    return {
        "status": "success", 
        "message": f"流捕获已停止 for {client_id}",
        "pool_stats": decoder_pool.get_stats()
    }


@router.get("/decoder_stats")
async def get_decoder_stats():
    """
    获取解码器进程池统计信息
    
    GET /inspection/decoder_stats
    """
    decoder_pool = get_decoder_pool()
    return decoder_pool.get_stats()