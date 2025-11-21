from contextlib import asynccontextmanager
from fastapi import FastAPI

from app.routers import ai, inspection, task


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # AI服务的生命周期由ai路由器管理
    async with ai.lifespan():
        yield


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