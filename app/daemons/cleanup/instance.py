"""TTL 清理 worker 全局单例（唯一定义处）

`worker.py` 只管类定义，要那一个全局实例的人才 import 本模块。构造不起线程；
是否启动由包 `lifespan()` 按 `enable_cleanup` 决定。
"""

from .config import get_cleanup_config
from .worker import CleanupWorker

_config = get_cleanup_config()

cleanup_worker: CleanupWorker = CleanupWorker(
    db_dir=_config.storage_base_dir,
    cleanup_days=_config.cleanup_days,
    interval_seconds=_config.cleanup_interval_seconds,
)
