"""
HLS持久化Worker池

负责并行处理HLS持久化任务
"""

import logging
import threading
from pathlib import Path
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
    """HLS持久化Worker池"""

    def __init__(
        self,
        input_queue: Queue,
        num_workers: int = 2,
        db_dir: Path | None = None,
    ):
        self.input_queue = input_queue
        self.num_workers = num_workers
        self.stop_event = threading.Event()

        # 创建持久化策略（编码帧率全程从帧 ts 自适应反推，不接收上游 fps）
        self.strategy = HLSPersistenceStrategy(
            db_dir=db_dir or Path("database"),
        )

        # 创建Worker
        self.workers = []
        self.threads = []

    def start(self):
        """启动Worker池"""
        logger.info("[HLSWorkerPool] Starting %d workers", self.num_workers)

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

    def release_dir_locks(self, task_id: int) -> int:
        """回收该 task 的 HLS 目录锁（转发到 strategy），返回回收数量。"""
        return self.strategy.release_dir_locks(task_id)

    def purge_step_dir(self, task_id: int, step_id: int) -> bool:
        """清空该 (task_id, step_id) step 目录（转发到 strategy），返回是否删除。"""
        return self.strategy.purge_step_dir(task_id, step_id)
