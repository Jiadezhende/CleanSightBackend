"""告警落库 sink（inference/temporal 域）。

由告警产出方（TemporalActor 实时 / settlement 结算）调用：把一批告警**过闸门 +
记入 CQ 环形缓冲 + 落库**串成一次编排。

职责划分：
- 过闸/入队（`cq.append_alarm_record_with_gate`）—— client 领域（CQ 自有去重闸门 +
  前端轮询的告警环形缓冲），闸门原子性归 CQ；
- 落库（`persistence_manager.persist_alarm(dict)`）—— persistence 无状态落库。

sink 只负责把两者编排到一起,不反向侵入任一方内部状态。persistence 因此不再依赖 cq。
（原 persist_alarms 一度迁入 PersistenceManager,现按"落库≠过闸编排"归位回产出域。）
"""

import logging
from typing import List

from app.domain.alarm import Alarm
from app.services.persistence import persistence_manager

logger = logging.getLogger(__name__)


def persist_alarms(alarms: List[Alarm], *, cq, mode: str, log_each: bool = False) -> None:
    """把一批告警过闸门后落库 + 记入内存环形缓冲（实时/结算共用一条映射）。

    别名已由产出方（temporal actor）烧进 alarm.stage，此处直接读、不反向 import inference.naming；
    metric 直接读 alarm.metric（产出方已填）；client_id / task_id / step_id 均由 cq 派生。
    顺序先内存后外部：内存日志供前端实时轮询，外部库本就 30s 批次。
    """
    task_id = cq.task_id
    step_id = cq.step_id
    client_id = cq.source_ip

    for alarm in alarms:
        # 给产出方的同一份告警补 mode，再过闸门+入环形缓冲（seq 由其赋；stage 已烧）。
        # 闸门 task_id 取自 cq 自身不可变身份，无需再传。
        alarm.mode = mode
        if not cq.append_alarm_record_with_gate(alarm, mode):
            continue
        persistence_manager.persist_alarm({
            "task_id": task_id,
            "stage": alarm.stage,
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
