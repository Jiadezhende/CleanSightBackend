"""
Persistence模块

独立的持久化服务，负责：
1. HLS视频段 + 推理数据持久化
2. 批量告警信息上报
"""

from .manager import PersistenceManager
from .config import PersistenceConfig
from .models import HLSPersistenceTask, AlarmPersistenceTask, PersistenceMetrics

__all__ = [
    'PersistenceManager',
    'PersistenceConfig',
    'HLSPersistenceTask',
    'AlarmPersistenceTask',
    'PersistenceMetrics',
]
