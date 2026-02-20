"""
Persistence模块

独立的持久化服务，负责：
1. HLS视频段 + 推理数据持久化
2. 批量告警信息上报
"""

from .config import PersistenceConfig, get_persistence_config
from .manager import PersistenceManager
from .models import AlarmPersistenceTask, HLSPersistenceTask, PersistenceMetrics

__all__ = [
    "PersistenceManager",
    "PersistenceConfig",
    "get_persistence_config",
    "HLSPersistenceTask",
    "AlarmPersistenceTask",
    "PersistenceMetrics",
]
