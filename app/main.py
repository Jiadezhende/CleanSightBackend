import logging
import os
import sys
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response

from app.routers import ai, api, health, inspection, task
from app.utils import (
    AppError,
    ConflictError,
    DatabaseError,
    FFmpegError,
    ModelInferenceError,
    NotFoundError,
    PersistenceError,
    StreamConnectionError,
    ValidationError,
)
from app.utils.metrics import get_metrics

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
    env = os.environ.get("CLEANSIGHT_ENV", "dev")
    env_names = {
        "dev": "开发环境 (.env.dev)",
        "test": "测试环境 (.env.test)",
        "prod": "生产环境 (.env)",
    }
    env_display = env_names.get(env, f"未知环境 ({env})")
    print(f"环境: {env_display}")

    # 显示关键配置
    print(f"数据库: {settings.db_host}:{settings.db_port}/{settings.db_name}")
    print(f"严格模式: {os.environ.get('CLEANSIGHT_STRICT', '0') == '1'}")
    print(f"调试模式: {settings.debug}")
    print("=" * 60 + "\n")

    # 按照服务模块启动生命周期管理
    # 1. 健康监控服务（依赖 client_manager, stream_service, inference_manager）
    # 2. AI 推理服务
    async with health.lifespan():
        async with ai.lifespan():
            yield


app = FastAPI(
    title="CleanSight Backend",
    description="AI-powered inspection of the endoscope cleaning process at Changhai Hospital",
    version="1.0.0",
    lifespan=lifespan,
)

# 注册路由器
app.include_router(api.router)  # 统一API（优先注册）
app.include_router(health.router)  # 健康监控
app.include_router(ai.router)
app.include_router(inspection.router)
app.include_router(task.router)


# ============================================================================
# 边界层 3: FastAPI 全局异常处理器
# ============================================================================

logger = logging.getLogger(__name__)


@app.exception_handler(StreamConnectionError)
async def stream_error_handler(request: Request, exc: StreamConnectionError):
    """
    流连接错误处理器（边界层 3）

    职责：
    1. 捕获所有 StreamConnectionError 异常
    2. 记录错误日志（包含完整上下文）
    3. 转换为 HTTP 503 状态码（Service Unavailable）
    """
    logger.error(
        f"[BoundaryLayer3] Stream connection error: {exc}",
        exc_info=True,
        extra={
            "client_id": exc.client_id,
            "url": str(request.url),
            "method": request.method,
        },
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "Stream unavailable",
            "detail": str(exc),
            "client_id": exc.client_id,
        },
    )


@app.exception_handler(FFmpegError)
async def ffmpeg_error_handler(request: Request, exc: FFmpegError):
    """
    FFmpeg 错误处理器（边界层 3）

    职责：
    1. 捕获所有 FFmpegError 异常
    2. 记录错误日志
    3. 转换为 HTTP 500 状态码（Internal Server Error）
    """
    logger.error(
        f"[BoundaryLayer3] FFmpeg error: {exc}",
        exc_info=True,
        extra={
            "client_id": exc.client_id,
            "url": str(request.url),
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "FFmpeg error",
            "detail": str(exc),
            "client_id": exc.client_id,
        },
    )


@app.exception_handler(DatabaseError)
async def database_error_handler(request: Request, exc: DatabaseError):
    """
    数据库错误处理器（边界层 3）

    职责：
    1. 捕获所有 DatabaseError 异常
    2. 记录错误日志
    3. 转换为 HTTP 503 状态码（Service Unavailable）
    """
    logger.error(
        f"[BoundaryLayer3] Database error: {exc}",
        exc_info=True,
        extra={
            "client_id": exc.client_id,
            "url": str(request.url),
        },
    )
    return JSONResponse(
        status_code=503,
        content={
            "error": "Database unavailable",
            "detail": str(exc),
            "retryable": exc.retryable,
        },
    )


@app.exception_handler(ModelInferenceError)
async def inference_error_handler(request: Request, exc: ModelInferenceError):
    """
    模型推理错误处理器（边界层 3）

    职责：
    1. 捕获所有 ModelInferenceError 异常
    2. 记录错误日志
    3. 转换为 HTTP 500 状态码（Internal Server Error）
    """
    logger.error(
        f"[BoundaryLayer3] Model inference error: {exc}",
        exc_info=True,
        extra={
            "client_id": exc.client_id,
            "url": str(request.url),
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Inference failed",
            "detail": str(exc),
            "client_id": exc.client_id,
        },
    )


@app.exception_handler(PersistenceError)
async def persistence_error_handler(request: Request, exc: PersistenceError):
    """
    持久化错误处理器（边界层 3）

    职责：
    1. 捕获所有 PersistenceError 异常
    2. 记录错误日志
    3. 转换为 HTTP 500 状态码（Internal Server Error）
    """
    logger.error(
        f"[BoundaryLayer3] Persistence error: {exc}",
        exc_info=True,
        extra={
            "client_id": exc.client_id,
            "url": str(request.url),
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Persistence failed",
            "detail": str(exc),
            "retryable": exc.retryable,
        },
    )


@app.exception_handler(NotFoundError)
async def not_found_error_handler(request: Request, exc: NotFoundError):
    """
    资源不存在处理器（边界层 3）

    职责：
    1. 捕获所有 NotFoundError 异常
    2. 记录警告日志（404 通常不是严重错误）
    3. 转换为 HTTP 404 状态码（Not Found）
    """
    logger.warning(
        f"[BoundaryLayer3] Resource not found: {exc}",
        extra={
            "resource_type": exc.resource_type,
            "resource_id": exc.resource_id,
            "url": str(request.url),
        },
    )
    return JSONResponse(
        status_code=404,
        content={
            "error": "Resource not found",
            "detail": str(exc),
            "resource_type": exc.resource_type,
            "resource_id": exc.resource_id,
        },
    )


@app.exception_handler(ValidationError)
async def validation_error_handler(request: Request, exc: ValidationError):
    """
    参数验证失败处理器（边界层 3）

    职责：
    1. 捕获所有 ValidationError 异常
    2. 记录警告日志（客户端错误，不记录 exc_info）
    3. 转换为 HTTP 400 状态码（Bad Request）
    """
    logger.warning(
        f"[BoundaryLayer3] Validation error: {exc}",
        extra={
            "field": exc.field,
            "value": exc.value,
            "url": str(request.url),
        },
    )
    return JSONResponse(
        status_code=400,
        content={
            "error": "Validation error",
            "detail": str(exc),
            "field": exc.field,
        },
    )


@app.exception_handler(ConflictError)
async def conflict_error_handler(request: Request, exc: ConflictError):
    """
    资源冲突处理器（边界层 3）

    职责：
    1. 捕获所有 ConflictError 异常
    2. 记录警告日志（客户端错误，不记录 exc_info）
    3. 转换为 HTTP 409 状态码（Conflict）
    """
    logger.warning(
        f"[BoundaryLayer3] Resource conflict: {exc}",
        extra={
            "client_id": exc.client_id,
            "resource_type": exc.resource_type,
            "resource_id": exc.resource_id,
            "url": str(request.url),
        },
    )
    return JSONResponse(
        status_code=409,
        content={
            "error": "Resource conflict",
            "detail": str(exc),
            "client_id": exc.client_id,
            "resource_type": exc.resource_type,
            "resource_id": exc.resource_id,
        },
    )


@app.exception_handler(AppError)
async def cleansight_exception_handler(request: Request, exc: AppError):
    """
    CleanSight 通用异常处理器（边界层 3）

    职责：
    1. 捕获所有 AppError 异常（未被具体处理器捕获的）
    2. 记录错误日志
    3. 转换为 HTTP 500 状态码（Internal Server Error）
    """
    logger.error(
        f"[BoundaryLayer3] CleanSight exception: {exc}",
        exc_info=True,
        extra={
            "client_id": exc.client_id,
            "url": str(request.url),
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal error",
            "detail": str(exc),
            "retryable": exc.retryable,
        },
    )


@app.exception_handler(Exception)
async def generic_exception_handler(request: Request, exc: Exception):
    """
    兜底异常处理器（边界层 3）

    职责：
    1. 捕获所有未被具体处理器捕获的异常
    2. 记录错误日志（包含完整 traceback）
    3. 转换为 HTTP 500 状态码（Internal Server Error）
    4. 防止敏感信息泄露（不返回完整异常信息）
    """
    logger.error(
        f"[BoundaryLayer3] Uncaught exception: {exc}",
        exc_info=True,
        extra={
            "url": str(request.url),
            "method": request.method,
            "exception_type": type(exc).__name__,
        },
    )
    return JSONResponse(
        status_code=500,
        content={
            "error": "Internal server error",
            "detail": "An unexpected error occurred. Please contact support if the issue persists.",
        },
    )


@app.get("/")
async def root():
    return {"message": "Welcome to CleanSight Backend"}


@app.get("/metrics")
async def metrics():
    """
    Prometheus metrics 端点

    返回所有 metrics（文本格式），供 Prometheus 服务器抓取

    用途：
        - Prometheus 配置：scrape_configs.static_configs.targets = ["localhost:8000/metrics"]
        - Grafana 可视化
        - 告警规则

    Metrics 包括：
        - infer_latency_ms: 推理延迟（Histogram）
        - infer_failure_total: 推理失败计数
        - frame_drop_total: 帧丢弃计数
        - gpu_oom_total: GPU OOM 计数
        - retry_total: 重试计数
    """
    return Response(content=get_metrics(), media_type="text/plain")


# ============================================================================
# 边界层 4: 顶层 Fail-Fast
# ============================================================================


def main():
    """
    应用入口（边界层 4: 顶层 Fail-Fast）

    职责：
    1. 捕获所有未处理异常
    2. 记录 CRITICAL 级别日志
    3. 优雅退出（Fail-Fast）

    使用方式：
        python -m app.main
    """
    try:
        logger.info("=" * 60)
        logger.info("Starting CleanSight Backend...")
        logger.info("=" * 60)

        # 启动 FastAPI 应用
        import uvicorn

        # 从环境变量读取配置
        host = os.environ.get("HOST", "0.0.0.0")
        port = int(os.environ.get("PORT", "8000"))
        log_config = os.environ.get("LOG_CONFIG", "logging_config.json")

        logger.info(f"Listening on {host}:{port}")
        logger.info(f"Log config: {log_config}")

        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            log_config=log_config,
            reload=False,  # 生产环境禁用热重载
        )

    except KeyboardInterrupt:
        logger.info("\n" + "=" * 60)
        logger.info("Received shutdown signal (Ctrl+C), exiting...")
        logger.info("=" * 60)
        sys.exit(0)

    except Exception as e:
        # 顶层边界捕获所有未处理异常
        logger.critical(
            "=" * 60 + "\n" + f"[BoundaryLayer4] Fatal error in main: {e}\n" + "=" * 60,
            exc_info=True,
        )
        # Fail-Fast: 记录日志后退出
        sys.exit(1)


if __name__ == "__main__":
    main()
