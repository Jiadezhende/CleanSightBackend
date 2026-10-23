"""告警落库 sink（inference/temporal 域）。

由告警产出方（TemporalActor 实时 / settlement 结算）调用：把一批告警**过闸门 +
记入本 run 的告警账本 + 落库**串成一次编排。

职责划分：
- 过闸/入账（`AlarmLedger.record`）—— 每 run 一本账（冷却闸门 + 前端轮询的环形日志），
  按 `Session` 弱引用挂在本模块的表上，随 session 对象回收；
- 落库（`alarm_service.persist_alarm(dict)`）—— alarm 服务无状态上报。

读账本：`ledger_for(sess).snapshot(since_seq)` / `.recent(n)`（routers 装配前端消息用）。
"""

import logging
import threading
import weakref
from typing import List

from app.types.alarm import Alarm
from app.services.alarm.instance import alarm_service
from app.services.session.core import Session, SessionState
from .alarm_ledger import AlarmLedger

logger = logging.getLogger(__name__)

# 每 run 状态挂 Session 的唯一显式例外（session/mq 提案 A9）：键是弱引用，session 对象回收即释放
_ledgers: "weakref.WeakKeyDictionary[Session, AlarmLedger]" = weakref.WeakKeyDictionary()
_ledgers_lock = threading.Lock()   # L2：只护 get-or-create


def ledger_for(sess: Session) -> AlarmLedger:
    """该 run 的告警账本（首次访问时建）。"""
    with _ledgers_lock:
        ledger = _ledgers.get(sess)
        if ledger is None:
            ledger = _ledgers[sess] = AlarmLedger()
        return ledger


def persist_alarms(alarms: List[Alarm], *, sess: Session, mode: str, log_each: bool = False) -> None:
    """把一批告警过闸门后记入账本 + 落库（实时/结算共用一条映射）。

    别名已由产出方（temporal actor）烧进 alarm.stage，此处直接读、不反向 import inference.naming；
    metric 直接读 alarm.metric（产出方已填）；task_id / step_id 由 sess 派生。
    写门：只有 CLOSED 拒——拆除期（DRAINING）的结算告警仍要入账。
    顺序先内存后外部：内存日志供前端实时轮询，外部库本就 30s 批次。
    """
    task_id = sess.info.run.task_id
    step_id = sess.info.run.step_id
    ledger = ledger_for(sess)

    for alarm in alarms:
        # 给产出方的同一份告警补 mode，再过闸门+入账（seq 由账本赋；stage 已烧）。
        alarm.mode = mode
        if sess.state is SessionState.CLOSED or not ledger.record(alarm, mode):
            continue
        alarm_service.persist_alarm({
            "task_id": task_id,
            "stage": alarm.stage,
            "step_id": step_id,
            "alarm_type": alarm.alarm_type,
            "alarm_metric": alarm.metric,
            "alarm_mode": mode,
            "alarm_level": alarm.alarm_level,
            "alarm_message": alarm.alarm_message,
            "detection_result": alarm.metadata if alarm.metadata else None,
        })
        if log_each:
            logger.info(
                "[alarm_sink] %s alarm for task=%s step=%s: %s", mode, task_id, step_id, alarm.alarm_message
            )
