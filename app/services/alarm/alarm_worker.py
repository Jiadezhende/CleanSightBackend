"""
告警上报Worker池

负责：
- 异步消费告警队列
- 告警上报（单条上报带重试：`_report_with_retry`）
"""

import logging
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable

from app.services.utils.worker_guard import guarded_run
from app.types.exceptions import AppError

from .reporter import AlarmReporter
from .types import AlarmReportTask

logger = logging.getLogger(__name__)

# 重试策略：最多尝试 3 次，指数退避 1s → 2s → …，单次等待上限 30s
_MAX_ATTEMPTS = 3
_BASE_DELAY = 1.0
_BACKOFF_FACTOR = 2.0
_MAX_DELAY = 30.0

# retry_total 的 operation 标签值（Prometheus 对外契约，沿用旧策略名，不改）
_RETRY_OPERATION = "persistence"


def _should_retry(exc: AppError, attempts: int) -> bool:
    """fatal=True 不重试；retryable=True 且尝试次数未达上限才重试。"""
    if exc.fatal:
        return False
    return exc.retryable and attempts < _MAX_ATTEMPTS


def _retry_delay(attempts: int) -> float:
    """第 `attempts` 次失败后的等待秒数。"""
    return min(_BASE_DELAY * (_BACKOFF_FACTOR ** (attempts - 1)), _MAX_DELAY)


def _record_exception(exc: Exception) -> None:
    from app.services.utils.metrics import retry_total

    retry_total.labels(operation=_RETRY_OPERATION, error_type=type(exc).__name__).inc()


def _report_with_retry(func: Callable[[], Any]) -> Any:
    """执行 `func`，AppError 按上面的策略重试；重试耗尽 / 致命 / 未知异常向上抛。"""
    attempts = 0

    while True:
        try:
            result = func()
            if attempts > 0:
                from app.services.utils.metrics import retry_total

                retry_total.labels(operation=_RETRY_OPERATION, error_type="recovered").inc()
            return result

        except AppError as e:
            attempts += 1
            retry = _should_retry(e, attempts)
            _record_exception(e)

            if retry:
                delay = _retry_delay(attempts)
                logger.warning(
                    "[AlarmWorker] Retry %d/%d after %.2fs (error=%s)",
                    attempts, _MAX_ATTEMPTS, delay, str(e)[:100],
                )
                time.sleep(delay)
                continue

            logger.error("[AlarmWorker] Fatal error: %s", e, exc_info=True)
            raise

        except Exception as e:
            logger.critical("[AlarmWorker] Unhandled exception: %s", e, exc_info=True)
            _record_exception(e)
            raise


class AlarmWorker:
    """告警上报Worker"""

    def __init__(
        self,
        input_queue: Queue,
        reporter: AlarmReporter,
        stop_event: threading.Event,
        worker_id: int = 0,
    ):
        self.input_queue = input_queue
        self.reporter = reporter
        self.stop_event = stop_event
        self.worker_id = worker_id

    def run(self):
        """工作循环"""
        logger.debug("[AlarmWorker-%d] Started", self.worker_id)

        while not self.stop_event.is_set():
            try:
                try:
                    task: AlarmReportTask = self.input_queue.get(timeout=0.5)
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

    def _process(self, task: AlarmReportTask) -> None:
        alarm_dict = task.to_dict()
        try:
            _report_with_retry(lambda: self.reporter.report_alarm(alarm_dict))
        except Exception as e:
            logger.error(
                "[AlarmWorker-%d] Report failed after retries: %s",
                self.worker_id,
                e,
                exc_info=True,
            )


class AlarmWorkerPool:
    """告警上报Worker池"""

    def __init__(
        self,
        input_queue: Queue,
        num_workers: int = 1,
    ):
        self.input_queue = input_queue
        self.num_workers = num_workers
        self.stop_event = threading.Event()
        self.reporter = AlarmReporter()
        self.workers = []
        self.threads = []

    def start(self):
        """启动Worker池"""
        logger.info("启动 %d 个告警Worker", self.num_workers)

        for i in range(self.num_workers):
            worker = AlarmWorker(
                input_queue=self.input_queue,
                reporter=self.reporter,
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
