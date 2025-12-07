"""
RTMP流管理API路由
提供RTMP流的管理和监控接口
"""

from fastapi import APIRouter, HTTPException, BackgroundTasks
from typing import List, Dict, Any, Optional
from pydantic import BaseModel
import logging

from ..services.rtmp_service import rtmp_service

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/rtmp", tags=["RTMP流管理"])

class StreamRequest(BaseModel):
    """流请求模型"""
    stream_name: str
    description: Optional[str] = None

class StreamResponse(BaseModel):
    """流响应模型"""
    name: str
    url: str
    status: str
    message: Optional[str] = None

class StreamStats(BaseModel):
    """流统计信息模型"""
    name: str
    url: str
    status: str
    start_time: Optional[str] = None
    frame_count: int = 0
    last_activity: Optional[str] = None
    is_running: Optional[bool] = None
    error_count: Optional[int] = None
    fps: Optional[float] = None

@router.get("/status")
async def get_service_status() -> Dict[str, Any]:
    """
    获取RTMP服务状态
    
    Returns:
        dict: RTMP服务状态信息
    """
    try:
        status = rtmp_service.check_service_status()
        return {
            "success": True,
            "data": status
        }
    except Exception as e:
        logger.error(f"获取RTMP服务状态失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/streams")
async def list_streams() -> Dict[str, Any]:
    """
    获取活跃流列表
    
    Returns:
        dict: 活跃流列表
    """
    try:
        streams = rtmp_service.list_active_streams()
        return {
            "success": True,
            "data": streams,
            "count": len(streams)
        }
    except Exception as e:
        logger.error(f"获取流列表失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/streams/{stream_name}")
async def get_stream_info(stream_name: str) -> Dict[str, Any]:
    """
    获取指定流的信息
    
    Args:
        stream_name: 流名称
        
    Returns:
        dict: 流信息
    """
    try:
        stream_stats = rtmp_service.get_stream_stats(stream_name)
        
        if stream_stats is None:
            raise HTTPException(status_code=404, detail=f"流不存在: {stream_name}")
        
        return {
            "success": True,
            "data": stream_stats
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取流信息失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/streams/{stream_name}/url")
async def get_stream_url(stream_name: str) -> Dict[str, Any]:
    """
    获取流URL
    
    Args:
        stream_name: 流名称
        
    Returns:
        dict: 流URL信息
    """
    try:
        stream_url = rtmp_service.get_stream_url(stream_name)
        
        return {
            "success": True,
            "data": {
                "stream_name": stream_name,
                "rtmp_url": stream_url,
                "push_url": stream_url,  # 推流地址
                "pull_url": stream_url   # 拉流地址
            }
        }
    except Exception as e:
        logger.error(f"获取流URL失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/streams/{stream_name}/test")
async def test_stream_connection(stream_name: str, timeout: int = 10) -> Dict[str, Any]:
    """
    测试流连接
    
    Args:
        stream_name: 流名称
        timeout: 超时时间（秒）
        
    Returns:
        dict: 测试结果
    """
    try:
        is_connected = rtmp_service.test_stream_connection(stream_name, timeout)
        
        return {
            "success": True,
            "data": {
                "stream_name": stream_name,
                "connected": is_connected,
                "message": "连接成功" if is_connected else "连接失败或超时"
            }
        }
    except Exception as e:
        logger.error(f"测试流连接失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/streams/{stream_name}/start")
async def start_stream_processor(
    stream_name: str,
    background_tasks: BackgroundTasks
) -> StreamResponse:
    """
    启动流处理器
    
    Args:
        stream_name: 流名称
        background_tasks: 后台任务
        
    Returns:
        StreamResponse: 启动结果
    """
    try:
        # 检查流是否已经在处理
        if stream_name in rtmp_service.stream_processors:
            return StreamResponse(
                name=stream_name,
                url=rtmp_service.get_stream_url(stream_name),
                status="running",
                message="流处理器已在运行"
            )
        
        # 启动流处理器
        success = rtmp_service.start_stream_processor(stream_name)
        
        if success:
            return StreamResponse(
                name=stream_name,
                url=rtmp_service.get_stream_url(stream_name),
                status="started",
                message="流处理器启动成功"
            )
        else:
            raise HTTPException(status_code=400, detail="流处理器启动失败")
            
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"启动流处理器失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.post("/streams/{stream_name}/stop")
async def stop_stream_processor(stream_name: str) -> StreamResponse:
    """
    停止流处理器
    
    Args:
        stream_name: 流名称
        
    Returns:
        StreamResponse: 停止结果
    """
    try:
        success = rtmp_service.stop_stream_processor(stream_name)
        
        if success:
            return StreamResponse(
                name=stream_name,
                url=rtmp_service.get_stream_url(stream_name),
                status="stopped",
                message="流处理器已停止"
            )
        else:
            return StreamResponse(
                name=stream_name,
                url=rtmp_service.get_stream_url(stream_name),
                status="not_found",
                message="流处理器不存在"
            )
            
    except Exception as e:
        logger.error(f"停止流处理器失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/streams/{stream_name}/stats")
async def get_stream_stats(stream_name: str) -> Dict[str, Any]:
    """
    获取流统计信息
    
    Args:
        stream_name: 流名称
        
    Returns:
        dict: 流统计信息
    """
    try:
        stats = rtmp_service.get_stream_stats(stream_name)
        
        if stats is None:
            raise HTTPException(status_code=404, detail=f"流不存在: {stream_name}")
        
        return {
            "success": True,
            "data": stats
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"获取流统计失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/config")
async def get_rtmp_config() -> Dict[str, Any]:
    """
    获取RTMP配置信息
    
    Returns:
        dict: RTMP配置信息
    """
    try:
        config = {
            "rtmp_host": rtmp_service.rtmp_host,
            "rtmp_port": rtmp_service.rtmp_port,
            "rtmp_base_url": rtmp_service.rtmp_base_url,
            "stat_url": f"http://{rtmp_service.rtmp_host}/stat",
            "health_url": f"http://{rtmp_service.rtmp_host}/health"
        }
        
        return {
            "success": True,
            "data": config
        }
    except Exception as e:
        logger.error(f"获取RTMP配置失败: {e}")
        raise HTTPException(status_code=500, detail=str(e))

@router.get("/health")
async def health_check() -> Dict[str, Any]:
    """
    RTMP服务健康检查
    
    Returns:
        dict: 健康检查结果
    """
    try:
        service_status = rtmp_service.check_service_status()
        is_healthy = service_status.get('status') == 'running'
        
        return {
            "success": True,
            "healthy": is_healthy,
            "service_status": service_status,
            "active_streams": len(rtmp_service.active_streams)
        }
    except Exception as e:
        logger.error(f"RTMP健康检查失败: {e}")
        return {
            "success": False,
            "healthy": False,
            "error": str(e)
        }