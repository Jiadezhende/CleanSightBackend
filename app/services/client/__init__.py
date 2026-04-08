"""
客户端管理模块

提供客户端队列和管理器的统一接口
"""

from .manager import ClientManager, client_manager
from .queues import ClientQueues

__all__ = [
    "ClientQueues",
    "ClientManager",
    "client_manager",
]
