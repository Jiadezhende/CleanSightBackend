"""
HLS持久化Worker池

负责并行处理HLS持久化任务
"""

import logging
import threading
from pathlib import Path
from queue import Empty, Queue

from app.services.persistence.models import HLSPersistenceTask
from app.services.persistence.strategies.hls_strategy import HLSPersistenceStrategy
from app.utils import GuardedExecutor

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
                            client_id=task.client_id,
                            task_id=task.task_id,
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
        segment_duration: int = 10,
        raw_fps: float = 30.0,
        processed_fps: float = 20.0,
    ):
        self.input_queue = input_queue
        self.num_workers = num_workers
        self.stop_event = threading.Event()

        # 创建持久化策略
        self.strategy = HLSPersistenceStrategy(
            db_dir=db_dir or Path("database"),
            raw_fps=raw_fps,
            processed_fps=processed_fps,
            enable_db_write=False,  # 不写入file_path表
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
            thread = threading.Thread(target=worker.run, daemon=True)

            self.workers.append(worker)
            self.threads.append(thread)
            thread.start()

    def stop(self, timeout: float = 10.0):
        """停止Worker池"""
        logger.debug("停止HLS Worker池")
        self.stop_event.set()

        for thread in self.threads:
            thread.join(timeout=timeout)

    def flush_client(self, _client_id: str):
        """预留接口：客户端断连时调用"""
        pass
