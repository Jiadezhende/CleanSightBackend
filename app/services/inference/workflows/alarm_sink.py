"""告警落库公共出口 —— 实时(REALTIME)与结算(SETTLEMENT)共用一条持久化映射。

L4 Judge 产出的 AlarmInfo 已自带 metric(产出方显式填,不靠文案反推),此处只做:
    去重闸门(try_pass_alarm_gate) → persist_alarm(外部库) → append_alarm_record(内存环形缓冲)。

两个调用点(temporal 实时 / manager 结算)此前逐行重复,差异仅在 mode、stage 来源、
client_id 来源、是否逐条记日志 —— 全部收进本函数的参数。
"""

from __future__ import annotations

import logging
from typing import List

from app.services.inference.data_models import Alarm
from app.services.inference.models import AlarmRecord

logger = logging.getLogger(__name__)


def persist_alarms(
    alarms: List[Alarm],
    *,
    cq,                       # ClientQueues
    client_id: str,
    stage_name: str,          # 已解析的可读别名(get_stage_alias 的结果)
    mode: str,                # ALARM_MODE_REALTIME / "SETTLEMENT"
    persistence_manager,
    log_each: bool = False,
) -> None:
    """把一批告警过闸门后落库 + 记入内存环形缓冲。

    metric 直接读 alarm.metric(产出方已填);task_id/step_id 由 cq 派生。
    """
    task_id = cq.get_task_id()
    task = cq.get_task()
    step_id = int(task.current_step) if task and task.current_step else None

    for alarm in alarms:
        metric = alarm.metric
        if not cq.try_pass_alarm_gate(task_id, metric, mode):
            continue
        persistence_manager.persist_alarm({
            "task_id": task_id,
            "stage": stage_name,
            "step_id": step_id,
            "client_id": client_id,
            "alarm_type": alarm.alarm_type,
            "alarm_metric": metric,
            "alarm_mode": mode,
            "alarm_level": alarm.alarm_level,
            "alarm_message": alarm.alarm_message,
            "detection_result": alarm.metadata if alarm.metadata else None,
        })
        cq.append_alarm_record(AlarmRecord(
            alarm_type=alarm.alarm_type,
            alarm_level=alarm.alarm_level,
            alarm_message=alarm.alarm_message,
            mode=mode,
            metric=metric,
            stage=stage_name,
            metadata=alarm.metadata or {},
        ))
        if log_each:
            logger.info(
                "[alarm_sink] %s alarm for %s: %s", mode, client_id, alarm.alarm_message
            )
