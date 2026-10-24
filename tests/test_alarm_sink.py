"""alarm_sink.persist_alarms 行为守卫（过闸编排从 AlarmService 移回 actor 域后）。

覆盖两条不变式：
1. persist_alarms 直接读 alarm.stage（别名已由 actor 前烧），不反向解析 alias；
2. 过闸被拒（冷却窗口）时跳过落库。

task_id / step_id 均由 session 派生，落库调用打到 sink 内部 import 的
alarm_service.persist_alarm，测试用 monkeypatch 拦截该出口。
"""

from unittest.mock import MagicMock

from factories import make_alarm
from app.types.run import RunIdentity
from app.types.alarm import AlarmMetric
from app.services.inference.online.temporal import alarm_sink
from app.services.session.core import Session, SessionInfo


def _sess():
    return Session(SessionInfo(run=RunIdentity(7, 1, 1), source_ip="c"))


def test_persist_alarms_reads_baked_stage(monkeypatch):
    captured = []
    monkeypatch.setattr(
        alarm_sink.alarm_service,
        "persist_alarm",
        lambda d: captured.append(d) or True,
    )

    sess = _sess()  # 新账本，首条必过闸

    alarm = make_alarm(metric=AlarmMetric.BUBBLE, stage="长刷")  # stage = 产出方已烧的可读别名

    alarm_sink.persist_alarms([alarm], sess=sess, mode="SETTLEMENT")

    assert len(captured) == 1
    assert captured[0]["stage"] == "长刷"          # 直接读 alarm.stage，未做 alias 解析
    assert captured[0]["alarm_metric"] == AlarmMetric.BUBBLE
    assert alarm.mode == "SETTLEMENT"              # mode 由 sink 补


def test_persist_alarms_gate_reject_skips_persist(monkeypatch):
    captured = []
    monkeypatch.setattr(
        alarm_sink.alarm_service,
        "persist_alarm",
        lambda d: captured.append(d) or True,
    )

    sess = _sess()
    ledger = MagicMock()
    ledger.record.return_value = False  # 冷却窗口拦截
    monkeypatch.setattr(alarm_sink, "ledger_for", lambda _sess: ledger)

    alarm = make_alarm()
    alarm_sink.persist_alarms([alarm], sess=sess, mode="REALTIME")

    assert captured == []  # 被闸门挡下，不落库


def test_ledger_is_per_session_and_weakly_held(monkeypatch):
    import gc

    monkeypatch.setattr(alarm_sink.alarm_service, "persist_alarm", lambda d: True)
    a, b = _sess(), _sess()
    alarm_sink.persist_alarms([make_alarm()], sess=a, mode="REALTIME")
    assert len(alarm_sink.ledger_for(a).recent()) == 1
    assert alarm_sink.ledger_for(b).recent() == []      # 账本按 run 隔离
    before = len(alarm_sink._ledgers)
    del a
    gc.collect()
    assert len(alarm_sink._ledgers) == before - 1       # session 回收即释放
