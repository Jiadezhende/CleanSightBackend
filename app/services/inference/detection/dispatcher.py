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
from typing import Deque, Dict, List, Optional

from app.services.client import ClientManager, ClientQueues, client_manager
from app.services.inference.models import DetectionTask
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


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

        # Stage分组队列：{stage: deque[DetectionTask]}
        self._stage_queues: Dict[str, Deque[DetectionTask]] = defaultdict(
            lambda: deque(maxlen=256)
        )
        self._lock = threading.Lock()

        # 统计信息
        self._stats = {
            "total_dispatched": 0,
            "by_stage": defaultdict(int),
        }

        # 各 stage 因 maxlen 满而静默淘汰最旧帧的累计计数（推理掉速时的真实积压信号）
        self._stage_drops: Dict[str, int] = defaultdict(int)

        # 推理压力日志（[INFER_PRESSURE]）：每 ~10s 评估一次，但仅在有压力时才打
        # （有丢帧 delta，或队列深度逼近上限），平稳时静默，避免刷屏。
        self._round_counter: int = 0
        self._check_every_rounds: int = max(1, int(10.0 / self.fetch_interval))
        self._pressure_queue_ratio: float = 0.5  # 队列深度 ≥ maxlen*该比例 视为积压前兆
        # 上次打印时的累计丢帧，用于算 delta（"自上次报告以来丢了多少"）
        self._last_logged_stage_drops: Dict[str, int] = defaultdict(int)
        self._last_logged_processed_drops: Dict[str, int] = defaultdict(int)

    def start(self):
        """启动调度线程"""
        if self._dispatch_thread is not None and self._dispatch_thread.is_alive():
            return

        self._stop_event.clear()
        self._dispatch_thread = threading.Thread(
            target=guarded_run,
            args=(self._dispatch_loop, self._stop_event, "StageAwareDispatcher"),
            daemon=True,
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
                logger.error("[StageAwareDispatcher] Dispatch error: %s", e, exc_info=True)

            self._round_counter += 1
            if self._round_counter % self._check_every_rounds == 0:
                self._log_pressure_snapshot()

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
        # ClientManager.snapshot() 返回字典副本，迭代安全
        clients = self._client_manager.snapshot()
        for task_id, cq in clients.items():
            # 从 ca_ready 队列取一帧（FIFO，保证公平）
            # 使用封装方法，避免直接访问内部队列
            frame_data = cq.pop_ca_ready()
            if frame_data is None:
                # 队列为空或并发场景下被其他线程取走
                continue

            # 获取该 run 当前的 stage
            stage = self._get_client_stage(task_id, cq)

            # 构造推理请求：捕获该 CQ 句柄随请求同行，写回凭它投递、不反查
            # （cq 即当前 snapshot 迭代出的对象，与 pop_ca_ready()/get_stage() 同源）。
            req = DetectionTask(
                task_id=task_id,
                stage=stage,
                timestamp=frame_data.timestamp,
                frame=frame_data.frame,
                cq=cq,
            )

            # 按 stage 分组入队
            with self._lock:
                q = self._stage_queues[stage]
                # 队列已满 → append 会静默淘汰最旧帧，先计数（对齐 ca_raw 的 frames_dropped_raw）
                if q.maxlen is not None and len(q) >= q.maxlen:
                    self._stage_drops[stage] += 1
                q.append(req)
                self._stats["total_dispatched"] += 1
                self._stats["by_stage"][stage] += 1

    def _get_client_stage(self, task_id: int, cq: ClientQueues) -> str:
        """获取该 run 当前所处的 stage。"""
        return cq.get_stage()

    def get_batch_for_stage(
        self, stage: str, max_size: int = None, timeout_ms: float = 3.0 # type: ignore
    ) -> List[DetectionTask]:
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
            DetectionTask 列表（可能为空）
        """
        import time

        if max_size is None:
            max_size = self.max_batch_per_stage

        batch: List[DetectionTask] = []
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

    def queue_depth(self, stage: str) -> int:
        """获取指定 stage 的当前队列深度（线程安全）。

        供推理循环计算自适应超时使用，避免外部直接访问内部锁与队列。
        """
        with self._lock:
            return len(self._stage_queues.get(stage, ()))

    def get_stage_queue_depths(self) -> Dict[str, int]:
        """获取各 stage 队列深度（调试用）"""
        with self._lock:
            return {stage: len(queue) for stage, queue in self._stage_queues.items()}

    def get_stage_drops(self) -> Dict[str, int]:
        """获取各 stage 因 maxlen 满而静默淘汰的累计丢帧数。"""
        with self._lock:
            return dict(self._stage_drops)

    def _log_pressure_snapshot(self) -> None:
        """有压力时打一条 [INFER_PRESSURE] 行：stage 队列深度/丢帧 + 各客户端 ca_processed。

        与 stream 侧 [BACKPRESSURE] 行职责分离——后者只反映入口/录制队列（ca_ready/ca_raw，
        结构上几乎恒空），本行专门暴露推理链路真实积压点：_stage_queues 静默淘汰、ca_processed
        成帧压力。

        **仅在有压力时才打**，平稳时静默以免刷屏。判定为有压力 = 任一 stage 或 ca_processed
        自上次报告以来有新增丢帧（delta>0），或任一 stage 队列深度 ≥ maxlen*ratio（积压前兆）。
        drop 给累计值 + delta（delta 才是"此刻是否在丢"的信号）。

        日志失败绝不影响调度热路径——整体 try/except 包裹。
        """
        try:
            # stage 段：深度 + 容量 + 累计丢帧(delta)
            with self._lock:
                depths = {s: len(q) for s, q in self._stage_queues.items()}
                caps = {s: (q.maxlen or 0) for s, q in self._stage_queues.items()}
                drops = dict(self._stage_drops)

            pressured = False
            stage_parts: List[str] = []
            # 取队列与丢帧两侧 stage 并集（有丢帧必有队列，并集仅为稳妥兜底）
            for stage in sorted(set(depths) | set(drops)):
                depth = depths.get(stage, 0)
                cap = caps.get(stage, 0)
                cum = drops.get(stage, 0)
                delta = cum - self._last_logged_stage_drops.get(stage, 0)
                if delta > 0 or (cap > 0 and depth >= cap * self._pressure_queue_ratio):
                    pressured = True
                stage_parts.append(f"{stage} q={depth}/{cap} drop={cum}(+{delta})")

            # client 段：ca_processed 深度/容量/累计丢帧(delta)
            client_parts: List[str] = []
            processed_drops: Dict[str, int] = {}
            for task_id, cq in self._client_manager.snapshot().items():
                cum = cq.frames_dropped_processed
                processed_drops[task_id] = cum
                delta = cum - self._last_logged_processed_drops.get(task_id, 0)
                if delta > 0:
                    pressured = True
                client_parts.append(
                    f"{task_id} ca_processed={cq.get_ca_processed_length()}/"
                    f"{cq.get_ca_processed_capacity()} drop={cum}(+{delta})"
                )

            if not pressured:
                return  # 平稳，静默

            logger.info(
                "[INFER_PRESSURE] stages: %s || clients: %s",
                " | ".join(stage_parts) if stage_parts else "(none)",
                " | ".join(client_parts) if client_parts else "(none)",
            )
            # 仅在实际打印后推进基线，使 delta = 自上次报告以来的增量
            self._last_logged_stage_drops = defaultdict(int, drops)
            self._last_logged_processed_drops = defaultdict(int, processed_drops)
        except Exception as e:
            logger.debug("[StageAwareDispatcher] pressure snapshot failed: %s", e)
