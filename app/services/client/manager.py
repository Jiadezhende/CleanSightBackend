"""
客户端队列管理器单例模块

提供全局统一的客户端队列管理，支持：
- 创建和获取客户端队列实例
- 监控所有客户端队列深度
- 资源清理和生命周期管理
"""

import threading
from typing import Dict, Optional
import logging

from .queues import ClientQueues

logger = logging.getLogger("app.services.client.manager")


class ClientManager:
    """
    客户端队列管理器单例

    负责全局管理所有客户端的队列资源，实现：
    - 按需创建 ClientQueues 实例
    - 提供线程安全的访问接口
    - 集中监控队列状态
    - 统一资源清理
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        """单例模式：确保全局只有一个 ClientManager 实例"""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
        return cls._instance

    def __init__(self):
        """初始化 ClientManager（仅在首次创建时执行）"""
        # 避免重复初始化
        if hasattr(self, '_initialized'):
            return

        self._initialized = True
        self._clients: Dict[str, ClientQueues] = {}
        self._clients_lock = threading.Lock()

        # 默认配置参数
        self._default_rt_maxlen = 30  # 实时队列长度（约1秒@30fps）
        self._default_ca_segment_len = 300  # CA段长度（约10秒@30fps）
        self._default_ca_maxlen = 2700  # CA队列最大长度（约90秒@30fps）

        logger.info("ClientManager 单例已初始化")

    def get_client(self, client_id: str, **kwargs) -> ClientQueues:
        """
        获取或创建指定客户端的队列管理器

        Args:
            client_id: 客户端唯一标识
            **kwargs: ClientQueues 构造参数，可选：
                - client_id: str
                - resize_width: int (默认 640)
                - resize_height: int (默认 480)
                - inference_fps: int (默认 10)
                - rt_maxlen: int (默认 30)
                - ca_segment_len: int (默认 150)
                - ca_maxlen: int (默认 2700)

        Returns:
            ClientQueues 实例
        """
        with self._clients_lock:
            if client_id not in self._clients:
                # 使用默认参数或传入参数创建
                rt_maxlen = kwargs.get('rt_maxlen', self._default_rt_maxlen)
                ca_segment_len = kwargs.get('ca_segment_len', self._default_ca_segment_len)
                ca_maxlen = kwargs.get('ca_maxlen', self._default_ca_maxlen)

                # 创建新的 ClientQueues 实例
                client_queues = ClientQueues(
                    client_id=client_id,
                    rt_maxlen=rt_maxlen,
                    ca_segment_len=ca_segment_len,
                    ca_maxlen=ca_maxlen
                )

                # 设置可选参数
                if 'resize_width' in kwargs:
                    client_queues.resize_width = kwargs['resize_width']
                if 'resize_height' in kwargs:
                    client_queues.resize_height = kwargs['resize_height']
                if 'inference_fps' in kwargs:
                    client_queues.inference_fps = kwargs['inference_fps']

                self._clients[client_id] = client_queues
                logger.info(f"创建新客户端队列: {client_id}")

            return self._clients[client_id]

    def has_client(self, client_id: str) -> bool:
        """
        检查客户端是否存在

        Args:
            client_id: 客户端标识

        Returns:
            True 如果客户端存在
        """
        with self._clients_lock:
            return client_id in self._clients

    def remove_client(self, client_id: str, cleanup: bool = True) -> bool:
        """
        注销客户端，可选清理资源

        Args:
            client_id: 客户端标识
            cleanup: 是否清理队列资源（清空所有队列）

        Returns:
            True 如果成功移除，False 如果客户端不存在
        """
        with self._clients_lock:
            client_queues = self._clients.pop(client_id, None)

            if client_queues is None:
                logger.warning(f"尝试移除不存在的客户端: {client_id}")
                return False

            if cleanup:
                try:
                    client_queues.clear()
                    logger.info(f"客户端队列已清理: {client_id}")
                except Exception as e:
                    logger.error(f"清理客户端队列失败 {client_id}: {e}")

            logger.info(f"客户端已移除: {client_id}")
            return True

    def get_all_clients(self) -> Dict[str, ClientQueues]:
        """
        获取所有客户端的队列管理器（只读副本）

        Returns:
            客户端ID到ClientQueues的映射字典
        """
        with self._clients_lock:
            return dict(self._clients)

    def get_all_queue_depths(self) -> Dict[str, Dict[str, int]]:
        """
        获取所有客户端的队列深度统计

        Returns:
            格式：{client_id: {ca_ready: N, ca_raw: N, ca_processed: N, rt_processed: N}}
        """
        with self._clients_lock:
            result = {}
            for client_id, client_queues in self._clients.items():
                result[client_id] = client_queues.get_queue_depths()
            return result

    def get_client_count(self) -> int:
        """获取当前管理的客户端总数"""
        with self._clients_lock:
            return len(self._clients)

    def clear_all(self) -> None:
        """
        清空所有客户端资源（用于服务关闭时的全局清理）
        """
        with self._clients_lock:
            for client_id, client_queues in list(self._clients.items()):
                try:
                    client_queues.clear()
                except Exception as e:
                    logger.error(f"清理客户端 {client_id} 失败: {e}")

            self._clients.clear()
            logger.info("所有客户端资源已清理")

    def get_status_summary(self) -> Dict:
        """
        获取整体状态摘要（用于监控和调试）

        Returns:
            包含客户端数量、队列深度等信息的字典
        """
        with self._clients_lock:
            total_frames = 0
            clients_status = {}

            for client_id, client_queues in self._clients.items():
                depths = client_queues.get_queue_depths()
                clients_status[client_id] = depths
                total_frames += sum(depths.values())

            return {
                "client_count": len(self._clients),
                "total_queued_frames": total_frames,
                "clients": clients_status
            }


# 全局单例实例
client_manager = ClientManager()
