"""temporal.py - 时序分析工作线程池（定时轮询架构）。

职责：
- 按固定间隔（tick_interval）轮询所有活跃客户端
- 读取 ClientQueues.slide_window 执行时序分析
- 更新 ClientQueues.latest_temporal（events 列表）
- 产出告警 → persistence + ClientQueues.alarm_log
"""

import logging
import threading
import time
from typing import Any, Dict, List, Optional

from app.services.client import client_manager
from app.services.inference.models import AlarmRecord

logger = logging.getLogger(__name__)


class TemporalWorker:
    """时序分析工作线程（定时轮询模式）。

    独立于上游 InferenceLoop，按 tick_interval 遍历所有活跃客户端，
    读取各自的 slide_window 执行时序分析。
    """

    def __init__(
        self,
        stop_event: threading.Event,
        worker_id: int = 0,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        tick_interval: float = 1.0,
    ):
        self.stop_event = stop_event
        self.worker_id = worker_id
        self.stage_configs = stage_configs or {}
        self.tick_interval = tick_interval

    def run(self):
        """工作循环：固定间隔轮询所有客户端。"""
        logger.debug("[TemporalWorker-%d] Started (tick=%.1fs)", self.worker_id, self.tick_interval)

        while not self.stop_event.is_set():
            tick_start = time.time()
            try:
                self._tick()
            except Exception as e:
                logger.error("[TemporalWorker-%d] Tick exception: %s", self.worker_id, e, exc_info=True)

            # 睡眠至下一个 tick
            elapsed = time.time() - tick_start
            sleep_time = max(0, self.tick_interval - elapsed)
            if sleep_time > 0:
                self.stop_event.wait(sleep_time)

        logger.debug("[TemporalWorker-%d] Stopped", self.worker_id)

    def _tick(self):
        """一次轮询：遍历所有活跃客户端执行时序分析。"""
        all_clients = client_manager.get_all_clients()
        for client_id, cq in all_clients.items():
            try:
                self._process_client(client_id, cq)
            except Exception as e:
                logger.error(
                    "[TemporalWorker-%d] Error processing client %s: %s",
                    self.worker_id, client_id, e, exc_info=True,
                )

    def _process_client(self, client_id: str, cq) -> None:
        """处理单个客户端的时序分析。"""
        stage = cq.state.get_stage()
        stage_cfg = self.stage_configs.get(stage, {})
        tasks = stage_cfg.get("models", [])
        if not tasks:
            return

        all_events: List[str] = []
        all_alarms = []

        for task in tasks:
            window = cq.get_slide_window(task.name)
            if not window:
                continue

            # 时序分析：纯窗口分析，返回事件列表
            events = task.analyze_temporal(window)
            all_events.extend(events)

            # 告警评估：基于窗口 + state 计数器管理
            alarms = task.evaluate_alarms(window, cq.state)
            all_alarms.extend(alarms)

        # 更新 cq.latest_temporal（仅事件列表）
        cq.set_latest_temporal(all_events)

        # 告警 → persistence + alarm_log
        if all_alarms:
            self._persist_and_log_alarms(all_alarms, cq, client_id, stage)

    def _persist_and_log_alarms(
        self, alarms: list, cq, client_id: str, stage: str
    ) -> None:
        """将告警入队 persistence + 写入 cq.alarm_log。"""
        from app.services.persistence import persistence_manager

        task_id = cq.get_task_id()
        for alarm in alarms:
            # 持久化上报
            persistence_manager.persist_alarm({
                "task_id": task_id,
                "stage": stage,
                "client_id": client_id,
                "alarm_type": alarm.alarm_type,
                "alarm_level": alarm.alarm_level,
                "alarm_message": alarm.alarm_message,
                "detection_result": alarm.metadata if alarm.metadata else None,
            })

            # 内存告警日志
            record = AlarmRecord(
                alarm_type=alarm.alarm_type,
                alarm_level=alarm.alarm_level,
                alarm_message=alarm.alarm_message,
                metadata=alarm.metadata or {},
            )
            cq.append_alarm_record(record)


class TemporalWorkerPool:
    """时序分析线程池（定时轮询模式）。"""

    def __init__(
        self,
        num_workers: int = 1,
        stage_configs: Optional[Dict[str, Dict[str, Any]]] = None,
        tick_interval: float = 1.0,
    ):
        self.num_workers = num_workers
        self.stage_configs = stage_configs or {}
        self.tick_interval = tick_interval

        self._stop_event = threading.Event()
        self._workers: list[threading.Thread] = []

    def start(self):
        """启动线程池。"""
        for i in range(self.num_workers):
            worker = TemporalWorker(
                stop_event=self._stop_event,
                worker_id=i,
                stage_configs=self.stage_configs,
                tick_interval=self.tick_interval,
            )

            thread = threading.Thread(
                target=worker.run,
                daemon=True,
                name=f"TemporalWorker-{i}",
            )
            thread.start()
            self._workers.append(thread)

        logger.info(
            "[TemporalWorkerPool] Started %d workers (tick=%.1fs)",
            self.num_workers, self.tick_interval,
        )

    def stop(self):
        """停止线程池。"""
        self._stop_event.set()

        for thread in self._workers:
            thread.join(timeout=2.0)

        logger.debug("[TemporalWorkerPool] Stopped")
