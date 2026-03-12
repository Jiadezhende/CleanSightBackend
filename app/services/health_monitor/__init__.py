"""
健康监控模块

职责：
- 监控所有客户端的流健康状态
- 检测断流并自动重连
- 协调完整清理（Stream + Inference + ClientManager）
- 检测孤儿流（有 ClientQueues 但没有 Decoder）
"""

from app.services.health_monitor.config import (
    HealthMonitorConfig,
    get_health_monitor_config,
)
from app.services.health_monitor.monitor import GlobalHealthMonitor

__all__ = [
    "GlobalHealthMonitor",
    "HealthMonitorConfig",
    "get_health_monitor_config",
]
