"""
Persistence模块

独立的持久化服务，负责：
1. HLS视频段 + 推理数据持久化
2. 批量告警信息上报
"""

from contextlib import asynccontextmanager

from .config import PersistenceConfig, get_persistence_config
from .manager import PersistenceManager
from .models import AlarmPersistenceTask, HLSPersistenceTask

# 全局单例，与 client_manager 保持一致的使用模式。
# 生命周期由 lifespan()（下）驱动，不再由 InferenceManager 代管——持久化是平级服务。
persistence_manager: PersistenceManager = PersistenceManager()


@asynccontextmanager
async def lifespan():
    """persistence 服务生命周期（起于 inference 之前、停于 inference 之后）。

    在 main.py 中嵌套于 ai.lifespan 外层：inference.stop() 产出的结算告警 + HLS 残段 flush
    仍落到仍在跑的 persistence，再由此 finally 停 persistence 抽干队列——保序、不丢尾。
    """
    persistence_manager.start()
    try:
        yield
    finally:
        persistence_manager.stop(timeout=10.0)


__all__ = [
    "PersistenceManager",
    "PersistenceConfig",
    "get_persistence_config",
    "HLSPersistenceTask",
    "AlarmPersistenceTask",
    "persistence_manager",
    "lifespan",
]
