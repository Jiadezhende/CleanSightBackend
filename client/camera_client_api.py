"""
摄像头采集客户端 - 带REST API控制接口
提供HTTP API来控制摄像头的启动和停止
"""

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from typing import Optional
import uvicorn
from camera_client import CameraClient
import logging

# 配置日志
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# FastAPI应用
app = FastAPI(
    title="摄像头客户端API",
    description="控制摄像头采集和视频上传的REST API",
    version="1.0.0"
)

# 全局客户端实例
camera_client: Optional[CameraClient] = None


class StartRequest(BaseModel):
    """启动请求参数"""
    client_id: str
    server_url: str = "ws://localhost:8000/inspection/upload_stream"
    camera_id: int = 0
    fps: int = 30
    width: int = 640
    height: int = 480
    jpeg_quality: int = 70
    
    class Config:
        schema_extra = {
            "example": {
                "client_id": "camera_001",
                "server_url": "ws://localhost:8000/inspection/upload_stream",
                "camera_id": 0,
                "fps": 30,
                "width": 640,
                "height": 480,
                "jpeg_quality": 70
            }
        }


class StatusResponse(BaseModel):
    """状态响应"""
    is_running: bool
    client_id: Optional[str] = None
    elapsed_time: float = 0.0
    frames_sent: int = 0
    frames_success: int = 0
    frames_error: int = 0
    success_rate: float = 0.0
    average_fps: float = 0.0


@app.get("/")
async def root():
    """根路径"""
    return {
        "message": "摄像头客户端API",
        "version": "1.0.0",
        "endpoints": {
            "POST /start": "启动摄像头采集和上传",
            "POST /stop": "停止摄像头采集和上传",
            "GET /status": "获取客户端状态",
            "GET /health": "健康检查"
        }
    }


@app.get("/health")
async def health():
    """健康检查"""
    return {"status": "healthy"}


@app.post("/start", response_model=dict)
async def start_camera(request: StartRequest):
    """
    启动摄像头采集和视频上传
    
    - **client_id**: 客户端唯一标识符（必需）
    - **server_url**: WebSocket服务器地址
    - **camera_id**: 摄像头ID（0为默认摄像头）
    - **fps**: 采集帧率
    - **width**: 视频宽度
    - **height**: 视频高度
    - **jpeg_quality**: JPEG编码质量（1-100）
    """
    global camera_client
    
    # 检查是否已经在运行
    if camera_client and camera_client.is_active():
        raise HTTPException(
            status_code=400,
            detail=f"客户端已在运行中（Client ID: {camera_client.client_id}）"
        )
    
    try:
        # 创建新的客户端实例
        camera_client = CameraClient(
            client_id=request.client_id,
            server_url=request.server_url,
            camera_id=request.camera_id,
            fps=request.fps,
            jpeg_quality=request.jpeg_quality,
            frame_width=request.width,
            frame_height=request.height
        )
        
        # 启动客户端
        if not camera_client.start():
            camera_client = None
            raise HTTPException(
                status_code=500,
                detail="摄像头启动失败，请检查摄像头是否可用"
            )
        
        logger.info(f"✅ 客户端已通过API启动 (Client ID: {request.client_id})")
        
        return {
            "status": "success",
            "message": "摄像头已启动",
            "client_id": request.client_id,
            "server_url": request.server_url,
            "camera_id": request.camera_id,
            "fps": request.fps
        }
        
    except HTTPException:
        raise
    except Exception as e:
        camera_client = None
        logger.error(f"❌ 启动失败: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"启动失败: {str(e)}"
        )


@app.post("/stop")
async def stop_camera():
    """
    停止摄像头采集和视频上传
    """
    global camera_client
    
    if camera_client is None or not camera_client.is_active():
        raise HTTPException(
            status_code=400,
            detail="客户端未在运行"
        )
    
    try:
        # 获取停止前的统计信息
        stats = camera_client.get_stats()
        
        # 停止客户端
        camera_client.stop()
        
        logger.info(f"✅ 客户端已通过API停止 (Client ID: {camera_client.client_id})")
        
        # 清理客户端实例
        client_id = camera_client.client_id
        camera_client = None
        
        return {
            "status": "success",
            "message": "摄像头已停止",
            "client_id": client_id,
            "final_stats": stats
        }
        
    except Exception as e:
        logger.error(f"❌ 停止失败: {e}")
        raise HTTPException(
            status_code=500,
            detail=f"停止失败: {str(e)}"
        )


@app.get("/status", response_model=StatusResponse)
async def get_status():
    """
    获取客户端当前状态和统计信息
    """
    if camera_client is None:
        return StatusResponse(is_running=False)
    
    stats = camera_client.get_stats()
    
    return StatusResponse(
        is_running=stats["is_running"],
        client_id=camera_client.client_id if camera_client else None,
        elapsed_time=stats["elapsed_time"],
        frames_sent=stats["frames_sent"],
        frames_success=stats["frames_success"],
        frames_error=stats["frames_error"],
        success_rate=stats["success_rate"],
        average_fps=stats["average_fps"]
    )


@app.on_event("shutdown")
async def shutdown_event():
    """应用关闭时清理资源"""
    global camera_client
    if camera_client and camera_client.is_active():
        logger.info("⚠️  应用关闭，正在停止客户端...")
        camera_client.stop()
        camera_client = None


def main():
    """主函数"""
    import argparse
    
    parser = argparse.ArgumentParser(description='摄像头客户端API服务')
    parser.add_argument('--host', type=str, default='0.0.0.0',
                       help='API服务器地址 (默认: 0.0.0.0)')
    parser.add_argument('--port', '-p', type=int, default=8001,
                       help='API服务器端口 (默认: 8001)')
    parser.add_argument('--reload', action='store_true',
                       help='开启热重载（开发模式）')
    
    args = parser.parse_args()
    
    logger.info("=" * 60)
    logger.info("🚀 启动摄像头客户端API服务")
    logger.info("=" * 60)
    logger.info(f"地址: http://{args.host}:{args.port}")
    logger.info(f"API文档: http://{args.host}:{args.port}/docs")
    logger.info(f"重定向文档: http://{args.host}:{args.port}/redoc")
    logger.info("=" * 60)
    
    # 启动FastAPI服务
    uvicorn.run(
        "camera_client_api:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info"
    )


if __name__ == "__main__":
    main()
