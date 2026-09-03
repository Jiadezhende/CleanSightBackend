"""
健康监控模块

职责：
- 监控所有客户端的流健康状态
- 检测断流并自动重连
- 协调完整清理（Stream + Inference + ClientManager）
- 检测孤儿流（有 ClientQueues 但没有 Decoder）

生命周期由本包的 `lifespan()` 负责，`main.py` 嵌套调用；单例在 `instance.py`。
"""

import logging
from contextlib import asynccontextmanager

from app.services.health_monitor.config import (
    HealthMonitorConfig,
    get_health_monitor_config,
)
from app.services.health_monitor.manager import GlobalHealthMonitor

logger = logging.getLogger(__name__)

__all__ = [
    "GlobalHealthMonitor",
    "HealthMonitorConfig",
    "get_health_monitor_config",
    "lifespan",
]


@asynccontextmanager
async def lifespan():
    """健康监控服务生命周期管理

    单例 import 写在函数体内（规范 §3）：`__init__` 只是包的公开面，不该让「只想拿
    `HealthMonitorConfig` 这个 dataclass」的调用方连带构造出一个全局监控实例。
    """
    from app.services.health_monitor.instance import health_monitor

    # 配置与三个协作者在 start() 内现取（见 GlobalHealthMonitor._resolve_deps）；
    # 启动行也由 start() 自己打（那条报的是 cleanup_timeout——本线上唯一还在做判定的时限）。
    health_monitor.start()

    if logger.isEnabledFor(logging.DEBUG):
        logger.debug(
            "[GlobalHealthMonitor] Config: reconnect_interval=%.1fs, "
            "cleanup_timeout=%.1fs, orphan_timeout=%.1fs",
            health_monitor.config.reconnect_interval,
            health_monitor.config.cleanup_timeout,
            health_monitor.config.orphan_timeout,
        )

    try:
        yield
    finally:
        stats = health_monitor.get_stats()
        health_monitor.stop()
        logger.info(
            "[GlobalHealthMonitor] checks=%d, cleanups=%d, reconnects=%d",
            stats["checks"],
            stats["cleanups"],
            stats["reconnects"],
        )
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug(
                "[GlobalHealthMonitor] Full stats: disconnects=%d, reconnect_successes=%d, orphans=%d",
                stats["disconnects"],
                stats["reconnect_successes"],
                stats["orphans_detected"],
            )
