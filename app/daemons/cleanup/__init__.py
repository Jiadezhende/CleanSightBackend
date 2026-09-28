"""
存储 TTL 清理：按时钟扫描存储根，删除过期 step 目录（只依赖 `app.storage`）

本 `__init__` 不做 re-export，只有 docstring + lifespan()，消费方走深路径：

    单例  from app.daemons.cleanup.instance import cleanup_worker
    类    from app.daemons.cleanup.worker import CleanupWorker
    配置  from app.daemons.cleanup.config import CleanupConfig, get_cleanup_config
"""

from contextlib import asynccontextmanager

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan():
    """cleanup 生命周期：`enable_cleanup` 为假时不起线程。

    单例 import 写在函数体内（规范 §3）。
    """
    from .config import get_cleanup_config
    from .instance import cleanup_worker

    if not get_cleanup_config().enable_cleanup:
        yield
        return

    cleanup_worker.start()
    try:
        yield
    finally:
        cleanup_worker.stop(timeout=5.0)
