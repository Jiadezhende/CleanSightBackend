"""
告警持久化Worker池

负责：
- 批量去重
- 定期flush
- 告警上报
"""

import logging
import threading
import time
from queue import Empty, Queue

from app.services.persistence.models import AlarmPersistenceTask
from app.services.persistence.strategies.alarm_strategy import AlarmPersistenceStrategy

logger = logging.getLogger(__name__)


class AlarmWorker:
    """告警持久化Worker（处理聚合后的告警）"""

    def __init__(
        self,
        aggregated_queue: Queue,  # 聚合后的告警队列
        strategy: AlarmPersistenceStrategy,
        stop_event: threading.Event,
        worker_id: int = 0,
    ):
        self.aggregated_queue = aggregated_queue
        self.strategy = strategy
        self.stop_event = stop_event
        self.worker_id = worker_id

    def run(self):
        """工作循环"""
        logger.debug("[AlarmWorker-%d] Started", self.worker_id)

        while not self.stop_event.is_set():
            try:
                # 从队列获取聚合后的告警
                try:
                    alarm_dict = self.aggregated_queue.get(timeout=0.5)
                except Empty:
                    continue

                # 上报告警
                try:
                    self.strategy.report_alarm(alarm_dict)
                except Exception as e:
                    logger.error(
                        "[AlarmWorker-%d] Report failed: %s",
                        self.worker_id,
                        e,
                        exc_info=True,
                    )

            except Exception as e:
                logger.error(
                    "[AlarmWorker-%d] Exception: %s", self.worker_id, e, exc_info=True
                )

        logger.debug("[AlarmWorker-%d] Stopped", self.worker_id)


class AlarmFlushThread:
    """告警批量刷新线程"""

    def __init__(
        self,
        input_queue: Queue,  # 原始告警队列
        aggregated_queue: Queue,  # 聚合后的告警队列
        strategy: AlarmPersistenceStrategy,
        stop_event: threading.Event,
        batch_interval: int = 30,
    ):
        self.input_queue = input_queue
        self.aggregated_queue = aggregated_queue
        self.strategy = strategy
        self.stop_event = stop_event
        self.batch_interval = batch_interval

    def run(self):
        """批量刷新循环"""
        logger.debug("[AlarmFlushThread] Started")

        while not self.stop_event.is_set():
            try:
                # 1. 从输入队列消费原始告警，进行聚合
                timeout_at = time.time() + self.batch_interval
                while time.time() < timeout_at and not self.stop_event.is_set():
                    try:
                        task: AlarmPersistenceTask = self.input_queue.get(timeout=0.5)
                        task_key = task.get_key()

                        # 转换为字典
                        alarm_dict = {
                            "task_id": task.task_id,
                            "step_id": task.step_id,
                            "client_id": task.client_id,
                            "alarm_type": task.alarm_type,
                            "alarm_level": task.alarm_level,
                            "alarm_message": task.alarm_message,
                            "detection_result": task.detection_result,
                        }

                        # 聚合
                        self.strategy.aggregate_alarm(task_key, alarm_dict)
                    except Empty:
                        continue

                # 2. flush待处理告警
                to_report = self.strategy.flush_pending_alarms()
                for key, agg_alarm in to_report:
                    try:
                        self.aggregated_queue.put(agg_alarm)
                        logger.info(
                            "告警已聚合: %s, count=%d",
                            key,
                            agg_alarm.get("alarm_count", 1),
                        )
                    except Exception as e:
                        logger.error("聚合告警入队失败: %s", e, exc_info=True)

            except Exception as e:
                logger.error("批量刷新异常: %s", e, exc_info=True)

        # 最后一次flush
        try:
            to_report = self.strategy.flush_pending_alarms()
            for key, agg_alarm in to_report:
                self.aggregated_queue.put(agg_alarm)
        except Exception as e:
            logger.error("最终刷新失败: %s", e, exc_info=True)

        logger.debug("告警批量刷新线程已停止")


class AlarmWorkerPool:
    """告警持久化Worker池"""

    def __init__(
        self,
        input_queue: Queue,  # 原始告警队列
        num_workers: int = 1,
        batch_interval: int = 30,
        cooldown_seconds: int = 60,
        retry_times: int = 3,
        retry_backoff: float = 1.0,
    ):
        self.input_queue = input_queue
        self.num_workers = num_workers
        self.stop_event = threading.Event()

        # 聚合后的告警队列（内部队列）
        self.aggregated_queue: Queue = Queue()

        # 创建持久化策略
        self.strategy = AlarmPersistenceStrategy(
            batch_interval=batch_interval,
            cooldown_seconds=cooldown_seconds,
            retry_times=retry_times,
            retry_backoff=retry_backoff,
        )

        # 创建批量刷新线程
        self.flush_thread_worker = AlarmFlushThread(
            input_queue=self.input_queue,
            aggregated_queue=self.aggregated_queue,
            strategy=self.strategy,
            stop_event=self.stop_event,
            batch_interval=batch_interval,
        )
        self.flush_thread = None

        # 创建Worker
        self.workers = []
        self.threads = []

    def start(self):
        """启动Worker池"""
        logger.info("启动 %d 个告警Worker + 1个批量刷新线程", self.num_workers)

        # 启动批量刷新线程
        self.flush_thread = threading.Thread(
            target=self.flush_thread_worker.run, daemon=True
        )
        self.flush_thread.start()

        # 启动Worker
        for i in range(self.num_workers):
            worker = AlarmWorker(
                aggregated_queue=self.aggregated_queue,
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
        logger.debug("停止告警Worker池")
        self.stop_event.set()

        # 等待批量刷新线程
        if self.flush_thread:
            self.flush_thread.join(timeout=timeout)

        # 等待Worker
        for thread in self.threads:
            thread.join(timeout=timeout)
