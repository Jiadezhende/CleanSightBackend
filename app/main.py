from contextlib import asynccontextmanager
from fastapi import FastAPI
import logging
import os

from app.routers import ai, inspection, task

# 注意：日志配置由 uvicorn 的 --log-config 参数管理
# 参见: logging_config.json
# 这里只设置日志级别（从环境变量读取）
log_level = os.environ.get("LOG_LEVEL", "INFO").upper()
numeric_level = getattr(logging, log_level, logging.INFO)
logging.root.setLevel(numeric_level)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 显示启动配置信息
    from app.settings import settings

    print("\n" + "=" * 60)
    print("CleanSight Backend 配置检查")
    print("=" * 60)

    # 显示当前环境
    env = os.environ.get('CLEANSIGHT_ENV', 'dev')
    env_names = {
        'dev': '开发环境 (.env.dev)',
        'test': '测试环境 (.env.test)',
        'prod': '生产环境 (.env)'
    }
    env_display = env_names.get(env, f'未知环境 ({env})')
    print(f"环境: {env_display}")

    # 显示关键配置
    print(f"数据库: {settings.db_host}:{settings.db_port}/{settings.db_name}")
    print(f"严格模式: {os.environ.get('CLEANSIGHT_STRICT', '0') == '1'}")
    print(f"调试模式: {settings.debug}")
    print("=" * 60 + "\n")

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