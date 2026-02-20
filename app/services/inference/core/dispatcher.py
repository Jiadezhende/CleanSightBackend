"""Stage 感知的帧调度器。

职责：
- 轮询所有客户端的 ca_ready 队列
- 按 stage 分组批量取帧
- 保证流间公平（Round-Robin）
"""

from __future__ import annotations

import logging
import threading
from collections import defaultdict, deque
from typing import TYPE_CHECKING, Deque, Dict, List, Optional

from app.services.inference.models import InferenceRequest

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from app.services.client import ClientManager, ClientQueues

# 延迟导入避免循环依赖
from app.services.client import client_manager


class StageAwareDispatcher:
    """Stage感知的帧调度器（直接引用 ClientManager）。

    职责：
    - 轮询所有客户端的 ca_ready 队列
    - 按 stage 分组批量取帧
    - 保证流间公平（Round-Robin）
    
    改进点：
    - 直接引用全局 ClientManager，实时获取客户端列表
    - 无需手动刷新，自动同步客户端变化
    - 新客户端加入或离开无延迟
    """

    def __init__(
        self,
        max_batch_per_stage: int = 8,
        fetch_interval: float = 0.01,  # 10ms 轮询间隔
        client_manager_instance: Optional["ClientManager"] = None,
    ):
        """
        Args:
            max_batch_per_stage: 每个 stage 最大 batch 大小
            fetch_interval: 轮询间隔（秒）
            client_manager_instance: ClientManager 实例（可选，用于依赖注入测试）
        """
        self._client_manager = client_manager_instance or client_manager
        self.max_batch_per_stage = max_batch_per_stage
        self.fetch_interval = fetch_interval

        self._stop_event = threading.Event()
        self._dispatch_thread: Optional[threading.Thread] = None

        # Stage分组队列：{stage: deque[InferenceRequest]}
        self._stage_queues: Dict[str, Deque[InferenceRequest]] = defaultdict(
            lambda: deque(maxlen=256)
        )
        self._lock = threading.Lock()

        # 统计信息
        self._stats = {
            "total_dispatched": 0,
            "by_stage": defaultdict(int),
        }

    def start(self):
        """启动调度线程"""
        if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
            return

        self._stop_event.clear()
        self._dispatch_thread = threading.Thread(
            target=self._dispatch_loop, daemon=True
        )
        self._dispatch_thread.start()
        logger.debug(
            "[StageAwareDispatcher] Started | interval=%.1fms", self.fetch_interval*1000
        )

    def stop(self):
        """停止调度线程"""
        self._stop_event.set()
        if self._dispatch_thread is not None:
            self._dispatch_thread.join(timeout=2.0)
        logger.debug("[StageAwareDispatcher] Stopped")

    def _dispatch_loop(self):
        """调度循环：轮询所有客户端，按 stage 分组入队"""
        while not self._stop_event.is_set():
            try:
                self._fetch_and_dispatch_round()
            except Exception as e:
                print(f"[StageAwareDispatcher] 异常: {e}")
                import traceback

                traceback.print_exc()

            # 使用 Event.wait 可及时响应 stop 信号
            self._stop_event.wait(self.fetch_interval)

    def _fetch_and_dispatch_round(self):
        """一轮调度：轮询所有客户端，取帧并按 stage 分组。

        容错设计：
        - 动态从 ClientManager 获取最新客户端列表（实时同步）
        - 客户端动态添加/移除自动生效，无延迟
        - 队列为空时跳过，不影响其他客户端
        """
        # 动态获取客户端列表（实时同步，无需刷新）
        # ClientManager.get_all_clients() 返回字典副本，迭代安全
        clients = self._client_manager.get_all_clients()
        for client_id, cq in clients.items():
            # 从 ca_ready 队列取一帧（FIFO，保证公平）
            # 使用封装方法，避免直接访问内部队列
            frame_data = cq.pop_ca_ready()
            if frame_data is None:
                # 队列为空或并发场景下被其他线程取走
                continue

            # 获取该客户端当前的 stage
            stage = self._get_client_stage(client_id, cq)

            # 构造推理请求
            req = InferenceRequest(
                client_id=client_id,
                frame=frame_data.frame,
                timestamp=frame_data.timestamp,
                stage=stage,
                frame_data=frame_data,
            )

            # 按 stage 分组入队
            with self._lock:
                self._stage_queues[stage].append(req)
                self._stats["total_dispatched"] += 1
                self._stats["by_stage"][stage] += 1

    def _get_client_stage(self, client_id: str, cq: ClientQueues) -> str:
        """获取客户端当前所处的 stage。

        优先从 ClientState 读取，否则从 task 推断。
        """
        # 优先从 ClientState 读取（新架构）
        if hasattr(cq, "state") and cq.state is not None:
            return cq.state.get_stage()

        # 兼容旧架构：从 task 的 current_step 推断 stage
        if cq.task is not None:
            step = getattr(cq.task, "current_step", None)
            if step == "leak_test":
                return "LEAK"
            elif step == "cleaning":
                return "CLEAN"

        # 默认 stage
        return "LEAK"

    def get_batch_for_stage(
        self, stage: str, max_size: int = None, timeout_ms: float = 3.0 # type: ignore
    ) -> List[InferenceRequest]:
        """获取指定 stage 的一个 batch（支持超时等待）。

        策略：
        1. 立即检查队列，如果有 max_size 个数据，立即返回
        2. 否则，等待 timeout_ms，期间持续检查
        3. 超时后，返回当前已有的数据（可能不满）

        Args:
            stage: Stage 名称（LEAK/CLEAN/etc.）
            max_size: 最大 batch 大小，默认使用 self.max_batch_per_stage
            timeout_ms: 超时时间（毫秒），默认 3ms（针对小并发优化）

        Returns:
            InferenceRequest 列表（可能为空）
        """
        import time

        if max_size is None:
            max_size = self.max_batch_per_stage

        batch: List[InferenceRequest] = []
        start_time = time.time()

        while len(batch) < max_size:
            with self._lock:
                queue = self._stage_queues[stage]
                # 取出当前可用的数据
                available = min(max_size - len(batch), len(queue))
                for _ in range(available):
                    batch.append(queue.popleft())

            # 批次已满，立即返回
            if len(batch) >= max_size:
                break

            # 检查超时
            elapsed_ms = (time.time() - start_time) * 1000
            if elapsed_ms >= timeout_ms:
                break

            # 短暂休眠，避免空转
            time.sleep(0.001)  # 1ms

        return batch

    def get_stage_queue_depths(self) -> Dict[str, int]:
        """获取各 stage 队列深度（调试用）"""
        with self._lock:
            return {stage: len(queue) for stage, queue in self._stage_queues.items()}
