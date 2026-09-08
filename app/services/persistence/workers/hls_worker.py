"""
HLS持久化Worker池

负责并行处理HLS持久化任务
"""

import logging
import threading
from queue import Empty, Queue

from app.services.persistence.types import HLSPersistenceTask
from app.services.persistence.strategies.hls_strategy import HLSPersistenceStrategy
from app.utils import GuardedExecutor
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


class HLSWorker:
    """HLS持久化Worker"""

    def __init__(
        self,
        input_queue: Queue,
        strategy: HLSPersistenceStrategy,
        stop_event: threading.Event,
        worker_id: int = 0,
    ):
        self.input_queue = input_queue
        self.strategy = strategy
        self.stop_event = stop_event
        self.worker_id = worker_id
        self.executor = GuardedExecutor()  # 用于重试逻辑

    def run(self):
        """工作循环"""
        logger.debug("[HLSWorker-%d] Started", self.worker_id)

        while not self.stop_event.is_set():
            try:
                # 从队列获取任务
                try:
                    task: HLSPersistenceTask = self.input_queue.get(timeout=0.5)
                except Empty:
                    continue

                # 执行持久化（使用GuardedExecutor处理重试）
                try:
                    self.executor.execute(
                        func=lambda: self.strategy.persist_segment(
                            task_id=task.task_id,
                            step_id=task.step_id,
                            segment_type=task.segment_type,
                            frames=task.frames,
                        ),
                        policy_name="persistence",
                    )
                except Exception as e:
                    # GuardedExecutor重试后仍失败，记录错误
                    logger.error(
                        "[HLSWorker-%d] Persistence failed after retries: %s", self.worker_id, e, exc_info=True
                    )

            except Exception as e:
                logger.error("[HLSWorker-%d] Exception: %s", self.worker_id, e, exc_info=True)

        logger.debug("[HLSWorker-%d] Stopped", self.worker_id)


class HLSWorkerPool:
    """HLS 持久化 Worker 池 —— **固定单线程**。

    段落盘的正确性依赖「同一 step 的相邻段不能读到相同的累计 EXTINF」（否则两段 fragment 的
    tfdt 起点撞在一起，hls.js 播到第二段停在段尾不前进）。此前靠一张按 (task_id, step_id)
    索引的目录锁表守这条，现在靠**只有一个写者**守。

    **worker 数不做成配置项**：留一个能在 yaml 里改回 2 的旋钮，等于留了一个静默把 tfdt
    竞争放回来的开关——没有任何东西会报错。真要恢复并发，得连同锁一起想清楚再改这里。
    前提被打破时由 `hls.HlsConcurrentWrite` 响亮地报出来。
    """

    def __init__(self, input_queue: Queue):
        self.input_queue = input_queue
        self.num_workers = 1
        self.stop_event = threading.Event()

        # 创建持久化策略（编码帧率全程从帧 ts 自适应反推，不接收上游 fps）。
        # **不再传 db_dir**：存储根由 step_store 自解析（settings.storage_base_dir），
        # 此前它经 config.storage_base_dir → 这里 → strategy 绕两层，而那个属性
        # 本身就是 `return settings.storage_base_dir`。
        self.strategy = HLSPersistenceStrategy()

        # 创建Worker
        self.workers = []
        self.threads = []

    def start(self):
        """启动Worker池"""
        logger.info("[HLSWorkerPool] Starting %d worker", self.num_workers)

        for i in range(self.num_workers):
            worker = HLSWorker(
                input_queue=self.input_queue,
                strategy=self.strategy,
                stop_event=self.stop_event,
                worker_id=i,
            )
            thread = threading.Thread(
                target=guarded_run,
                args=(worker.run, self.stop_event, f"HLSWorker-{i}"),
                daemon=True,
            )

            self.workers.append(worker)
            self.threads.append(thread)
            thread.start()

    def stop(self, timeout: float = 10.0):
        """停止Worker池"""
        logger.debug("停止HLS Worker池")
        self.stop_event.set()

        for thread in self.threads:
            thread.join(timeout=timeout)

    def purge_step_dir(self, task_id: int, step_id: int) -> bool:
        """清空该 (task_id, step_id) step 目录（转发到 strategy），返回是否删除。"""
        return self.strategy.purge_step_dir(task_id, step_id)
