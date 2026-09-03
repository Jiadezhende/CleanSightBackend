"""
Persistence模块

独立的持久化服务，负责：
1. HLS视频段 + 推理数据持久化
2. 批量告警信息上报

本 `__init__` 不做 re-export（规范 §3 的「门面型」：只有 docstring + lifespan()）——
顶层 re-export `manager` 会经 `strategies` 把 cv2 摊给每个 import 本包的人。消费方走深路径：

    单例      from app.services.persistence.instance import persistence_manager
    类        from app.services.persistence.manager import PersistenceManager
    数据形状  from app.services.persistence.types import HLSPersistenceTask, AlarmPersistenceTask
    配置      from app.services.persistence.config import PersistenceConfig, get_persistence_config
"""

from contextlib import asynccontextmanager

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan():
    """persistence 服务生命周期（起于 inference 之前、停于 inference 之后）。

    在 main.py 中嵌套于 inference.lifespan 外层：inference.stop() 产出的结算告警 + HLS 残段 flush
    仍落到仍在跑的 persistence，再由此 finally 停 persistence 抽干队列——保序、不丢尾。

    单例 import 写在函数体内（规范 §3）：写在模块级就等于把上面那笔过路费又收回来。
    """
    from app.services.persistence.instance import persistence_manager

    persistence_manager.start()
    try:
        yield
    finally:
        persistence_manager.stop(timeout=10.0)
