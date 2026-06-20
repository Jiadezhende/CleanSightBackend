"""temporal.py - per-client 时序分析 Actor。

每个活跃 client 对应一个 ClientTemporalActor 实例，拥有独立线程。
actor 由 InferenceManager 在 set_task() 时创建，在 remove_client() 时停止。

职责：
- 持有该 client 所有 (TemporalAnalyzer, Judge) 配对（每 Analyzer/Judge 自带 self._sm）
- 按固定间隔（tick_interval）执行：analyzer.run 产事实 → judge.step 出告警
- 事实追加进 FactLedger；告警 → persistence + ClientQueues.alarm_log
- 在 finalize_and_stop() 时收集 judge 结算告警
"""

import logging
import threading
from typing import List, Optional, Tuple

from app.services.inference.data_models import ALARM_MODE_REALTIME, AlarmInfo
from app.services.inference.models import AlarmRecord, infer_alarm_metric
from app.services.inference.workflows.analyzer import TemporalAnalyzer
from app.services.inference.workflows.judge import Judge
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


class ClientTemporalActor:
    """per-client 时序分析 actor。

    每个 client 独立一个实例，故障互不影响。
    状态机（_sm）封装在各 Analyzer / Judge 内部，actor 不持有外部 sm 字典。
    """

    def __init__(
        self,
        client_id: str,
        cq,                              # ClientQueues
        stage: str,
        pairs: List[Tuple[TemporalAnalyzer, Optional[Judge]]],
        ledger=None,                     # FactLedger（可选）
        tick_interval: float = 1.0,
    ):
        self._client_id = client_id
        self._cq = cq
        self._stage = stage
        self._pairs = pairs
        self._ledger = ledger
        self._tick_interval = tick_interval

        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=guarded_run,
            args=(self._run, self._stop_event, f"TemporalActor-{client_id}"),
            daemon=True,
            name=f"TemporalActor-{client_id}",
        )

    def start(self) -> None:
        self._thread.start()
        logger.debug("[TemporalActor-%s] Started (tick=%.1fs)", self._client_id, self._tick_interval)

    def signal_stop(self) -> None:
        """向 actor 线程发送停止信号（非阻塞）。"""
        self._stop_event.set()

    def finalize_and_stop(self) -> List[AlarmInfo]:
        """停止 actor 线程，然后收集结算告警。

        调用方须确保在 remove_client() 流程中先调用此方法，
        再清理 ClientQueues，保证 _sm 读取时无并发写入。
        """
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        return self._collect_settlement_alarms()

    # ──────────────────────────────────────────
    # 内部实现
    # ──────────────────────────────────────────

    def _run(self) -> None:
        while not self._stop_event.wait(self._tick_interval):
            try:
                self._tick()
            except Exception as e:
                logger.error(
                    "[TemporalActor-%s] Tick error: %s", self._client_id, e, exc_info=True
                )
        logger.debug("[TemporalActor-%s] Stopped", self._client_id)

    def _tick(self) -> None:
        all_events: List[str] = []
        all_alarms: List[AlarmInfo] = []
        all_facts: list = []

        for analyzer, judge in self._pairs:
            window = self._cq.get_slide_window(analyzer.name)
            if not window:
                continue
            facts = analyzer.run(window, online=True)  # L3：只产事实
            if not facts:
                continue
            all_facts.extend(facts)
            if judge is not None:                       # L4：消费事实出告警
                events, alarms = judge.step(facts)
                all_events.extend(events)
                all_alarms.extend(alarms)

        self._cq.set_latest_temporal(all_events)

        if all_alarms:
            self._persist_alarms(all_alarms)

        # 事实落账本（best-effort，离线链路回读）
        if all_facts and self._ledger is not None:
            self._ledger.append(self._cq.get_task_id(), all_facts)

    def _persist_alarms(self, alarms: List[AlarmInfo]) -> None:
        from app.services.persistence import persistence_manager

        task = self._cq.get_task()
        task_id = self._cq.get_task_id()
        step_id = int(task.current_step) if task and task.current_step else None
        for alarm in alarms:
            metric = infer_alarm_metric(
                alarm_type=alarm.alarm_type,
                alarm_message=alarm.alarm_message,
                metadata=alarm.metadata or {},
            )
            if not self._cq.try_pass_alarm_gate(task_id, metric, ALARM_MODE_REALTIME):
                continue
            persistence_manager.persist_alarm({
                "task_id": task_id,
                "stage": self._stage,
                "step_id": step_id,
                "client_id": self._client_id,
                "alarm_type": alarm.alarm_type,
                "alarm_metric": metric,
                "alarm_mode": ALARM_MODE_REALTIME,
                "alarm_level": alarm.alarm_level,
                "alarm_message": alarm.alarm_message,
                "detection_result": alarm.metadata if alarm.metadata else None,
            })
            self._cq.append_alarm_record(AlarmRecord(
                alarm_type=alarm.alarm_type,
                alarm_level=alarm.alarm_level,
                alarm_message=alarm.alarm_message,
                mode=ALARM_MODE_REALTIME,
                metric=metric,
                stage=self._stage,
                metadata=alarm.metadata or {},
            ))

    def _collect_settlement_alarms(self) -> List[AlarmInfo]:
        alarms: List[AlarmInfo] = []
        for analyzer, judge in self._pairs:
            if judge is None:
                continue
            try:
                alarms.extend(judge.finalize())
            except Exception as e:
                logger.error(
                    "[TemporalActor-%s] finalize() failed for judge %s: %s",
                    self._client_id, judge.name, e, exc_info=True,
                )
        return alarms
