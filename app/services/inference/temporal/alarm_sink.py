"""告警落库公共出口 —— 实时(REALTIME)与结算(SETTLEMENT)共用一条持久化映射。

L4 Judge 产出的 AlarmInfo 已自带 metric(产出方显式填,不靠文案反推),此处只做:
    append_alarm_record_with_gate(闸门去重+入内存环形缓冲,原子) → persist_alarm(外部库)。
    顺序为先内存后外部:内存日志供前端实时轮询,外部库本就 30s 批次,先记内存更实时。

两个调用点(temporal 实时 / manager 结算)此前逐行重复,差异仅在 mode、stage 来源、
client_id 来源、是否逐条记日志 —— 全部收进本函数的参数。
"""

from __future__ import annotations

import logging
from typing import List

from app.domain.alarm import Alarm

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
        # 给产出方的同一份告警补落库字段，再过闸门+入环形缓冲（seq 由其赋）。
        alarm.mode = mode
        alarm.stage = stage_name
        if not cq.append_alarm_record_with_gate(task_id, alarm, mode):
            continue
        persistence_manager.persist_alarm({
            "task_id": task_id,
            "stage": stage_name,
            "step_id": step_id,
            "client_id": client_id,
            "alarm_type": alarm.alarm_type,
            "alarm_metric": alarm.metric,
            "alarm_mode": mode,
            "alarm_level": alarm.alarm_level,
            "alarm_message": alarm.alarm_message,
            "detection_result": alarm.metadata if alarm.metadata else None,
        })
        if log_each:
            logger.info(
                "[alarm_sink] %s alarm for %s: %s", mode, client_id, alarm.alarm_message
            )
