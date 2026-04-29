"""
告警持久化Worker池

负责：
- 异步消费告警队列
- 告警上报
"""

import logging
import threading
from queue import Empty, Queue

from app.services.persistence.models import AlarmPersistenceTask
from app.services.persistence.strategies.alarm_strategy import AlarmPersistenceStrategy
from app.utils import GuardedExecutor
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


class AlarmWorker:
    """告警持久化Worker"""

    def __init__(
        self,
        input_queue: Queue,
        strategy: AlarmPersistenceStrategy,
        stop_event: threading.Event,
        worker_id: int = 0,
    ):
        self.input_queue = input_queue
        self.strategy = strategy
        self.stop_event = stop_event
        self.worker_id = worker_id
        self.executor = GuardedExecutor()

    def run(self):
        """工作循环"""
        logger.debug("[AlarmWorker-%d] Started", self.worker_id)

        while not self.stop_event.is_set():
            try:
                try:
                    task: AlarmPersistenceTask = self.input_queue.get(timeout=0.5)
                except Empty:
                    continue

                self._process(task)

            except Exception as e:
                logger.error(
                    "[AlarmWorker-%d] Exception: %s", self.worker_id, e, exc_info=True
                )

        # 停止信号发出后 drain 队列剩余任务，避免告警丢失
        while True:
            try:
                task = self.input_queue.get_nowait()
                self._process(task)
            except Empty:
                break

        logger.debug("[AlarmWorker-%d] Stopped", self.worker_id)

    def _process(self, task: AlarmPersistenceTask) -> None:
        alarm_dict = task.to_dict()
        try:
            self.executor.execute(
                func=lambda: self.strategy.report_alarm(alarm_dict),
                policy_name="persistence",
            )
        except Exception as e:
            logger.error(
                "[AlarmWorker-%d] Report failed after retries: %s",
                self.worker_id,
                e,
                exc_info=True,
            )


class AlarmWorkerPool:
    """告警持久化Worker池"""

    def __init__(
        self,
        input_queue: Queue,
        num_workers: int = 1,
    ):
        self.input_queue = input_queue
        self.num_workers = num_workers
        self.stop_event = threading.Event()
        self.strategy = AlarmPersistenceStrategy()
        self.workers = []
        self.threads = []

    def start(self):
        """启动Worker池"""
        logger.info("启动 %d 个告警Worker", self.num_workers)

        for i in range(self.num_workers):
            worker = AlarmWorker(
                input_queue=self.input_queue,
                strategy=self.strategy,
                stop_event=self.stop_event,
                worker_id=i,
            )
            thread = threading.Thread(
                target=guarded_run,
                args=(worker.run, self.stop_event, f"AlarmWorker-{i}"),
                daemon=True,
            )
            self.workers.append(worker)
            self.threads.append(thread)
            thread.start()

    def stop(self, timeout: float = 10.0):
        """停止Worker池"""
        logger.debug("停止告警Worker池")
        self.stop_event.set()

        for thread in self.threads:
            thread.join(timeout=timeout)
