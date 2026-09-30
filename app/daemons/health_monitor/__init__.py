"""
健康监控：按时钟轮询各 run 的流健康，断流重连 / 任务超时 / 孤儿清理（拆除委托 RunControlService）

本 `__init__` 不做 re-export，只有 docstring + lifespan()，消费方走深路径：

    单例  from app.daemons.health_monitor.instance import health_monitor_worker
    类    from app.daemons.health_monitor.worker import HealthMonitorWorker
    配置  from app.daemons.health_monitor.config import HealthMonitorConfig, get_health_monitor_config
"""

import logging
from contextlib import asynccontextmanager

logger = logging.getLogger(__name__)

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan():
    """健康监控生命周期管理

    单例 import 写在函数体内（规范 §3）：不让只想拿 `HealthMonitorConfig` 的调用方
    连带构造出一个全局监控实例。
    """
    from .instance import health_monitor_worker

    # 配置与协作者在 start() 内现取（见 HealthMonitorWorker._resolve_deps）；
    # 启动行也由 start() 自己打（那条报的是 cleanup_timeout——本线上唯一还在做判定的时限）。
    health_monitor_worker.start()

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[HealthMonitorWorker] Config: reconnect_interval=%.1fs, "
            "cleanup_timeout=%.1fs, orphan_timeout=%.1fs",
            health_monitor_worker.config.reconnect_interval,
            health_monitor_worker.config.cleanup_timeout,
            health_monitor_worker.config.orphan_timeout,
        )

    try:
        yield
    finally:
        stats = health_monitor_worker.get_stats()
        health_monitor_worker.stop()
        logger.info(
            "[HealthMonitorWorker] checks=%d, cleanups=%d, reconnects=%d",
            stats["checks"],
            stats["cleanups"],
            stats["reconnects"],
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[HealthMonitorWorker] Full stats: disconnects=%d, reconnect_successes=%d, orphans=%d",
                stats["disconnects"],
                stats["reconnect_successes"],
                stats["orphans_detected"],
            )
