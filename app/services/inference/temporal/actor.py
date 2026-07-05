"""temporal.py - per-client 时序分析 Actor（流处理框架的执行上下文）。

每个活跃 client 对应一个 ClientTemporalActor 实例，拥有独立线程。
actor 由 InferenceManager 在 start_workflow() 时创建，在 stop_workflow() 时停止。

职责：
- 注册该 client 所有流算子 Operator（每个 Operator 自带共享状态机 self._sm）
- 按固定间隔（tick_interval）执行：按 subscribes 收集各订阅流 → operator.analyze 推进状态
  → operator.judge 出告警；per-operator 异常隔离，一个算子炸不影响同 tick 其余
- 告警 → persistence + ClientQueues.alarm_log；事实是离线概念（FeatureStore 特征 + 离线 worker）
- 在 finalize_and_stop() 时收集 operator 结算告警
"""

import logging
import threading
from typing import List

from app.domain.alarm import ALARM_MODE_REALTIME, Alarm
from app.services.inference.naming import get_stage_alias
from app.services.inference.temporal import alarm_sink
from app.services.inference.temporal.operator import Operator
from app.utils.worker_guard import guarded_run

logger = logging.getLogger(__name__)


class ClientTemporalActor:
    """per-client 时序分析 actor（注册多个流算子，1Hz 驱动）。

    每个 client 独立一个实例，故障互不影响。
    状态机（_sm）封装在各 Operator 内部，actor 不持有外部 sm 字典。
    """

    def __init__(
        self,
        task_id: int,
        cq,                              # ClientQueues
        stage: str,
        operators: List[Operator],
        tick_interval: float = 1.0,
    ):
        self._task_id = task_id
        self._cq = cq
        self._stage = stage
        # 别名前烧：stage 对本 run 不可变，构造期解析一次；产出告警即烧进 alarm.stage，
        # 下游持久化直接读 alarm.stage、不反向 import inference.naming。这是全仓唯一告警路径 alias 解析点。
        self._stage_alias = get_stage_alias(stage)
        self._operators = operators
        self._tick_interval = tick_interval

        self._stop_event = threading.Event()
        self._thread = threading.Thread(
            target=guarded_run,
            args=(self._run, self._stop_event, f"TemporalActor-{task_id}"),
            daemon=True,
            name=f"TemporalActor-{task_id}",
        )

    def start(self) -> None:
        self._thread.start()
        logger.debug("[TemporalActor-%s] Started (tick=%.1fs)", self._task_id, self._tick_interval)

    def signal_stop(self) -> None:
        """向 actor 线程发送停止信号（非阻塞）。"""
        self._stop_event.set()

    def finalize_and_stop(self) -> List[Alarm]:
        """停止 actor 线程，然后收集结算告警。

        调用方须确保在 stop_workflow() 流程中先调用此方法，
        再清理 ClientQueues，保证 _sm 读取时无并发写入。
        """
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        if self._thread.is_alive():
            logger.warning(
                "[TemporalActor-%s] tick 线程未在 2s 内退出（疑似卡在慢 analyze/persist），"
                "跳过结算以避免 _sm 并发读写", self._task_id,
            )
            return []
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
                    "[TemporalActor-%s] Tick error: %s", self._task_id, e, exc_info=True
                )
        logger.debug("[TemporalActor-%s] Stopped", self._task_id)

    def _tick(self) -> None:
        all_events: List[str] = []
        all_alarms: List[Alarm] = []

        for op in self._operators:
            try:
                # 按 subscribes 收集各订阅流的滑窗快照（流名 = detector.name）
                windows = {src: self._cq.get_slide_window(src) for src in op.subscribes}
                if not any(windows.values()):
                    continue
                op.analyze(windows)                 # 推进共享状态 _sm
                events, alarms = op.judge()         # 读 _sm 出 overlay + 告警
                all_events.extend(events)
                all_alarms.extend(alarms)
            except Exception as e:
                # per-operator 隔离：单个算子异常不影响同 tick 其余算子
                logger.error(
                    "[TemporalActor-%s] operator '%s' tick error: %s",
                    self._task_id, op.name, e, exc_info=True,
                )
                continue

        self._cq.set_latest_temporal(all_events)

        if all_alarms:
            for a in all_alarms:
                a.stage = self._stage_alias  # 别名前烧
            self._persist_alarms(all_alarms)

    def _persist_alarms(self, alarms: List[Alarm]) -> None:
        # 过闸 + 落库编排归 alarm_sink（别名已烧进 alarm.stage）
        alarm_sink.persist_alarms(alarms, cq=self._cq, mode=ALARM_MODE_REALTIME)

    def _collect_settlement_alarms(self) -> List[Alarm]:
        alarms: List[Alarm] = []
        for op in self._operators:
            try:
                alarms.extend(op.finalize())
            except Exception as e:
                logger.error(
                    "[TemporalActor-%s] finalize() failed for operator %s: %s",
                    self._task_id, op.name, e, exc_info=True,
                )
        for a in alarms:
            a.stage = self._stage_alias  # 别名前烧：结算告警离开 inference 前 .stage 即可读别名
        return alarms
