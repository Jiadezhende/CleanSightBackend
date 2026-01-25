"""
客户端管理模块

提供客户端状态、队列和管理器的统一接口
"""

from .state import ClientState
from .queues import ClientQueues
from .manager import ClientManager, client_manager

__all__ = [
    "ClientState",
    "ClientQueues",
    "ClientManager",
    "client_manager",
]
