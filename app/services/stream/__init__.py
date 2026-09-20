"""
流服务模块

提供视频流解码和管理的统一接口。

本包**懒启动、只收尸**：decoder 不在应用启动时拉起，而是由 `run_control` 按 run 现起
（`run_control.py` 的 start 路径），故 `lifespan()` 的启动段是空的，只在 finally 里
统一停掉所有残留 decoder。

本 `__init__` 不做 re-export（规范 §3 的「门面型」：只有 docstring + lifespan()），
消费方走深路径：

    单例      from app.services.stream.instance import stream_service
    类        from app.services.stream.manager import StreamService
    解码器    from app.services.stream.decoder import FFmpegDecoder
"""

import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan():
    """流服务生命周期管理（无启动段，只负责关停时收尸）

    单例 import 写在函数体内（规范 §3）：import 本包不该连带构造出 StreamService。
    """
    from app.services.stream.instance import stream_service

    try:
        yield
    finally:
        try:
            stream_service.shutdown()
            logger.info("[StreamService] Stream service stopped")
        except Exception:
            logger.exception("[StreamService] Error shutting down stream service")
