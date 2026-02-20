"""
客户端队列管理器单例模块

提供全局统一的客户端队列管理，支持：
- 创建和获取客户端队列实例
- 监控所有客户端队列深度
- 资源清理和生命周期管理
"""

import logging
import threading
from collections import defaultdict
from typing import Any, Dict, List

from .config import get_client_config
from .queues import ClientQueues

logger = logging.getLogger("app.services.client.manager")

# 加载客户端配置（单例）
_client_config = get_client_config()


class ClientManager:
    """
    客户端队列管理器（支持依赖注入，非强制单例）

    负责全局管理所有客户端的队列资源，实现：
    - 按需创建 ClientQueues 实例
    - 提供线程安全的访问接口（细粒度锁优化）
    - 集中监控队列状态
    - 统一资源清理

    改进点：
    - 移除单例强制，允许测试时创建多个实例
    - 细粒度锁：客户端级别锁 + 全局管理锁分离，减少锁竞争
    - 快速路径优化：已存在客户端的访问无需全局锁
    - 支持配置注入，便于单元测试
    """

    def __init__(self, config=None):
        """
        初始化 ClientManager

        Args:
            config: 可选配置对象（便于测试注入 mock 配置）
        """
        self._config = config or _client_config
        self._clients: Dict[str, ClientQueues] = {}

        # 细粒度锁策略
        self._clients_lock = threading.RLock()  # 管理字典的锁（可重入锁）
        self._client_locks: Dict[str, threading.RLock] = defaultdict(
            threading.RLock
        )  # 每个客户端独立锁

        # 默认配置参数（从配置加载）
        self._default_rt_maxlen = self._config.rt_maxlen  # 实时队列长度
        self._default_ca_segment_len = self._config.ca_segment_len  # CA段长度
        self._default_ca_maxlen = self._config.ca_maxlen  # CA队列最大长度
        self._default_inference_fps = self._config.inference_fps  # 推理采样频率

        logger.info("ClientManager 已初始化")

    def get_client(self, client_id: str, **kwargs) -> ClientQueues:
        """
        获取或创建指定客户端的队列管理器（细粒度锁优化）

        加锁策略：
        1. 快速路径：客户端已存在时无需全局锁，直接返回
        2. 慢速路径：需要创建时使用客户端级别锁（减少全局锁竞争）
        3. 双重检查：防止并发创建同一客户端

        Args:
            client_id: 客户端唯一标识
            **kwargs: ClientQueues 构造参数，可选：
                - resize_width: int (默认 640)
                - resize_height: int (默认 480)
                - inference_fps: int (默认 10)
                - rt_maxlen: int (默认 30)
                - ca_segment_len: int (默认 150)
                - ca_maxlen: int (默认 2700)

        Returns:
            ClientQueues 实例
        """
        # 快速路径：客户端已存在，无需全局锁（减少 90% 的锁竞争）
        if client_id in self._clients:
            return self._clients[client_id]

        # 慢速路径：需要创建客户端
        with self._client_locks[client_id]:  # 使用客户端级别锁
            # 双重检查（另一个线程可能已创建）
            if client_id in self._clients:
                return self._clients[client_id]

            # 使用默认参数或传入参数创建
            rt_maxlen = kwargs.get("rt_maxlen", self._default_rt_maxlen)
            ca_segment_len = kwargs.get("ca_segment_len", self._default_ca_segment_len)
            ca_maxlen = kwargs.get("ca_maxlen", self._default_ca_maxlen)
            inference_fps = kwargs.get("inference_fps", self._default_inference_fps)

            # 创建新的 ClientQueues 实例
            client_queues = ClientQueues(
                client_id=client_id,
                rt_maxlen=rt_maxlen,
                ca_segment_len=ca_segment_len,
                ca_maxlen=ca_maxlen,
                inference_fps=inference_fps,
            )

            # 设置可选参数
            if "resize_width" in kwargs:
                client_queues.resize_width = kwargs["resize_width"]
            if "resize_height" in kwargs:
                client_queues.resize_height = kwargs["resize_height"]

            # 注册到全局字典（短暂全局锁）
            with self._clients_lock:
                self._clients[client_id] = client_queues

            logger.info(f"创建新客户端队列: {client_id}")
            return client_queues

    def has_client(self, client_id: str) -> bool:
        """
        检查客户端是否存在（无锁快速路径）

        注：dict 的 __contains__ 在 CPython 中是原子操作，
        无需加锁（仅用于快速检查）

        Args:
            client_id: 客户端标识

        Returns:
            True 如果客户端存在
        """
        return client_id in self._clients

    def remove_client(self, client_id: str, cleanup: bool = True) -> Dict[str, Any]:
        """
        注销客户端，可选清理资源（客户端级别锁优化）

        加锁策略：
        1. 使用客户端级别锁保护整个移除流程
        2. 清理操作在全局锁外执行（减少锁持有时间）

        Args:
            client_id: 客户端标识
            cleanup: 是否清理队列资源（清空所有队列）

        Returns:
            清理结果字典，包含以下字段：
            - client_id: 客户端标识
            - removed: 是否成功移除
            - cleaned: 是否成功清理队列（仅当 cleanup=True 时有效）
            - error: 错误信息（如果有）
        """
        result = {
            "client_id": client_id,
            "removed": False,
            "cleaned": False,
            "error": None,
        }

        with self._client_locks[client_id]:  # 客户端级别锁
            with self._clients_lock:  # 短暂全局锁：只用于字典操作
                client_queues = self._clients.pop(client_id, None)

                if client_queues is None:
                    result["error"] = "client_not_found"
                    logger.warning(f"尝试移除不存在的客户端: {client_id}")
                    return result

                result["removed"] = True

            # 清理操作在全局锁外执行（减少锁持有时间）
            if cleanup:
                try:
                    client_queues.clear()
                    result["cleaned"] = True
                    logger.info(f"客户端队列已清理: {client_id}")
                except Exception as e:
                    result["error"] = str(e)
                    logger.error(f"清理客户端队列失败 {client_id}: {e}")

            logger.info(f"客户端已移除: {client_id}")

        # 清理锁字典（可选，防止内存泄漏）
        if client_id in self._client_locks:
            del self._client_locks[client_id]

        return result

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
        获取所有客户端的队列深度统计（读锁优化）

        加锁策略：
        1. 全局锁仅用于获取客户端ID列表快照
        2. 遍历读取各客户端状态时无全局锁（并行读取）

        Returns:
            格式：{client_id: {ca_ready: N, ca_raw: N, ca_processed: N, rt_processed: N}}
        """
        # 短暂全局锁：获取客户端ID快照
        with self._clients_lock:
            client_ids = list(self._clients.keys())

        # 并行读取各客户端状态（无全局锁，减少锁竞争）
        result = {}
        for cid in client_ids:
            if cid in self._clients:  # 防御性检查（可能已被删除）
                result[cid] = self._clients[cid].get_queue_depths()
        return result

    def get_client_count(self) -> int:
        """
        获取当前管理的客户端总数（原子操作，无需锁）

        注：len() 在 CPython 中是原子操作
        """
        return len(self._clients)

    def clear_all(self) -> List[Dict[str, Any]]:
        """
        清空所有客户端资源（用于服务关闭时的全局清理）

        加锁策略：
        1. 先获取客户端ID列表快照
        2. 逐个调用 remove_client（利用已有的客户端级锁）

        Returns:
            每个客户端的清理结果列表
        """
        # 获取客户端ID快照
        with self._clients_lock:
            client_ids = list(self._clients.keys())

        # 逐个移除（复用 remove_client 的细粒度锁逻辑）
        results = []
        for client_id in client_ids:
            result = self.remove_client(client_id, cleanup=True)
            results.append(
                {
                    "client_id": client_id,
                    "success": result["removed"] and result["cleaned"],
                    "error": result.get("error"),
                }
            )

        logger.info(f"所有客户端资源已清理 (总数: {len(results)})")
        return results

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
                "clients": clients_status,
            }


# 全局单例实例
client_manager = ClientManager()
