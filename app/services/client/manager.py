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
from typing import Any, Dict, List, Optional

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

    锁层级（Lock Hierarchy）：
      L_client = _client_locks[client_id]   threading.RLock，per-client
      L_global = _clients_lock              threading.RLock，全局注册表

    获取顺序始终为 L_client → L_global（当两者同时持有时）。
    此顺序与通常的"自顶向下"相反，原因：
    - L_client 是 get-or-create 的大临界区，防止同一 client 被重复创建
    - L_global 仅做短暂 dict 变更，嵌套在 L_client 内部
    - 没有任何代码路径先持 L_global 再持 L_client，因此不存在死锁风险

    仅持 L_global（不持 L_client）的路径：
      bind_task / get_client_by_task_id / get_all_clients / get_all_queue_depths / clear_all 快照段
    无锁路径（依赖 CPython GIL 原子性，见 get_client 注释）：
      has_client / get_client_count / get_client 快速路径
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

        # task_id 双向索引（由 _clients_lock 保护）
        self._task_to_client: Dict[int, str] = {}   # task_id → client_id
        self._client_to_task: Dict[str, int] = {}   # client_id → current task_id

        # 默认配置参数（从配置加载）
        self._default_ca_segment_len = self._config.ca_segment_len  # CA段长度
        self._default_ca_maxlen = self._config.ca_maxlen  # CA队列最大长度
        self._default_inference_fps = self._config.inference_fps  # 推理采样频率
        self._default_raw_fps = self._config.raw_fps  # 原始/解码帧率（抽帧降采样率分母）

        logger.info("[ClientManager] Initialized")

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
                - ca_segment_len: int (默认 150)
                - ca_maxlen: int (默认 2700)

        Returns:
            ClientQueues 实例
        """
        # 快速路径：CPython GIL 保证 dict.__contains__ 和 dict.__getitem__ 各自原子。
        # 接受的 TOCTOU：并发 remove_client() 可能在 check 之后、return 之前删除该条目，
        # 返回一个已被 clear() 的 ClientQueues。接受此风险，原因：
        #   1. remove_client() 仅在流关闭时调用，不在推理热路径
        #   2. 所有快速路径调用方在使用返回值前均检查 None 槽位，可容忍 stale 引用
        #   3. 对每次帧写入加全局锁成本不可接受
        if client_id in self._clients:
            return self._clients[client_id]

        # 慢速路径：需要创建客户端
        with self._client_locks[client_id]:  # 使用客户端级别锁
            # 双重检查（另一个线程可能已创建）
            if client_id in self._clients:
                return self._clients[client_id]

            # 使用默认参数或传入参数创建
            ca_segment_len = kwargs.get("ca_segment_len", self._default_ca_segment_len)
            ca_maxlen = kwargs.get("ca_maxlen", self._default_ca_maxlen)
            inference_fps = kwargs.get("inference_fps", self._default_inference_fps)
            raw_fps = kwargs.get("raw_fps", self._default_raw_fps)

            # 创建新的 ClientQueues 实例
            client_queues = ClientQueues(
                client_id=client_id,
                ca_segment_len=ca_segment_len,
                ca_maxlen=ca_maxlen,
                inference_fps=inference_fps,
                raw_fps=raw_fps,
            )

            # 设置可选参数
            if "resize_width" in kwargs:
                client_queues.resize_width = kwargs["resize_width"]
            if "resize_height" in kwargs:
                client_queues.resize_height = kwargs["resize_height"]

            # 注册到全局字典（短暂全局锁）
            with self._clients_lock:
                self._clients[client_id] = client_queues

            logger.info(f"Create New Client: {client_id}")
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

    def bind_task(self, client_id: str, task_id: int) -> None:
        """注册 task_id ↔ client_id 双向映射，自动淘汰同一 client 的旧任务绑定。"""
        with self._clients_lock:
            old_task = self._client_to_task.pop(client_id, None)
            if old_task is not None:
                self._task_to_client.pop(old_task, None)
            self._task_to_client[task_id] = client_id
            self._client_to_task[client_id] = task_id

    def get_client_by_task_id(self, task_id: int) -> Optional[ClientQueues]:
        """按 task_id 直接查 ClientQueues，无需经过 DB。"""
        with self._clients_lock:
            client_id = self._task_to_client.get(task_id)
            if client_id is None:
                return None
            return self._clients.get(client_id)

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
                old_task = self._client_to_task.pop(client_id, None)
                if old_task is not None:
                    self._task_to_client.pop(old_task, None)

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
                    logger.error("清理客户端队列失败 %s: %s", client_id, e, exc_info=True)

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
            格式：{client_id: {ca_ready: N, ca_raw: N, ca_processed: N, has_rendered: bool}}
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

        加锁策略：仅在持锁期间做 O(n) dict 快照，遍历和 per-client 调用在锁外执行，
        避免全局锁持有时间随客户端数量线性增长。
        """
        with self._clients_lock:
            snapshot = dict(self._clients)

        total_frames = 0
        clients_status = {}
        for client_id, client_queues in snapshot.items():
            depths = client_queues.get_queue_depths()
            clients_status[client_id] = depths
            total_frames += (
                depths.get("ca_ready", 0)
                + depths.get("ca_raw", 0)
                + depths.get("ca_processed", 0)
            )

        return {
            "client_count": len(snapshot),
            "total_queued_frames": total_frames,
            "clients": clients_status,
        }


# 全局单例实例
client_manager = ClientManager()
