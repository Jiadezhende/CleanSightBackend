"""
Persistence模块

独立的持久化服务，负责：
1. HLS视频段 + 推理数据持久化
2. 批量告警信息上报
"""

from .config import PersistenceConfig, get_persistence_config
from .manager import PersistenceManager
from .models import AlarmPersistenceTask, HLSPersistenceTask, PersistenceMetrics

# 全局单例，与 client_manager 保持一致的使用模式
# 生命周期由 InferenceManager 管理（调用 .start() / .stop()）
persistence_manager: PersistenceManager = PersistenceManager()

__all__ = [
    "PersistenceManager",
    "PersistenceConfig",
    "get_persistence_config",
    "HLSPersistenceTask",
    "AlarmPersistenceTask",
    "PersistenceMetrics",
    "persistence_manager",
]
