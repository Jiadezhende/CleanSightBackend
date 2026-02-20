"""持久化策略模块"""

from .alarm_strategy import AlarmPersistenceStrategy
from .hls_strategy import HLSPersistenceStrategy

__all__ = ["HLSPersistenceStrategy", "AlarmPersistenceStrategy"]
