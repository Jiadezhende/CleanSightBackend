from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.routers import ai, inspection, task
from app.services.decoder import (
    get_decoder_pool, 
    start_frame_dispatcher, 
    stop_frame_dispatcher,
    shutdown_decoder_pool
)
from app.services import ai as ai_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 1. 启动时：初始化解码器进程池
    print("[Startup] 初始化解码器进程池...")
    decoder_pool = get_decoder_pool()
    print(f"[Startup] 解码器进程池已初始化 (最大进程数: {decoder_pool.max_workers})")
    
    # 2. 启动时：启动帧分发器
    print("[Startup] 启动帧分发器...")
    start_frame_dispatcher(ai_service.submit_frame)
    print("[Startup] 帧分发器已启动")
    
    # 3. AI服务的生命周期由ai路由器管理
    async with ai.lifespan():
        yield
    
    # 4. 关闭时：停止帧分发器
    print("[Shutdown] 停止帧分发器...")
    stop_frame_dispatcher()
    
    # 5. 关闭时：关闭解码器进程池
    print("[Shutdown] 关闭解码器进程池...")
    shutdown_decoder_pool()
    print("[Shutdown] 清理完成")


app = FastAPI(
    title="CleanSight Backend",
    description="AI-powered inspection of the endoscope cleaning process at Changhai Hospital",
    version="1.0.0",
    lifespan=lifespan,
)

# 注册路由器
app.include_router(ai.router)
app.include_router(inspection.router)
app.include_router(task.router)


@app.get("/")
async def root():
    return {"message": "Welcome to CleanSight Backend"}