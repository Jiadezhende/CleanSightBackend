"""alarm_sink.persist_alarms 行为守卫（过闸编排从 PersistenceManager 移回 actor 域后）。

覆盖两条不变式：
1. persist_alarms 直接读 alarm.stage（别名已由 actor 前烧），不反向解析 alias；
2. 过闸被拒（冷却窗口）时跳过落库。

client_id / task_id / step_id 均由 cq 派生，落库调用打到 sink 内部 import 的
persistence_manager.persist_alarm，测试用 monkeypatch 拦截该出口。
"""

from types import SimpleNamespace
from unittest.mock import MagicMock

from app.domain.alarm import Alarm, AlarmMetric, AlarmType
from app.services.inference.temporal import alarm_sink


def test_persist_alarms_reads_baked_stage(monkeypatch):
    captured = []
    monkeypatch.setattr(
        alarm_sink.persistence_manager,
        "persist_alarm",
        lambda d: captured.append(d) or True,
    )

    cq = MagicMock()
    cq.task_id = 7
    cq.step_id = 1
    cq.source_ip = "c"
    cq.append_alarm_record_with_gate.return_value = True  # 过闸

    alarm = Alarm(
        alarm_type=AlarmType.MOCK, alarm_level="low", alarm_message="m",
        metric=AlarmMetric.BUBBLE,
    )
    alarm.stage = "长刷"  # 产出方已烧的可读别名

    alarm_sink.persist_alarms([alarm], cq=cq, mode="SETTLEMENT")

    assert len(captured) == 1
    assert captured[0]["stage"] == "长刷"          # 直接读 alarm.stage，未做 alias 解析
    assert captured[0]["alarm_metric"] == AlarmMetric.BUBBLE
    assert alarm.mode == "SETTLEMENT"              # mode 由 sink 补


def test_persist_alarms_gate_reject_skips_persist(monkeypatch):
    captured = []
    monkeypatch.setattr(
        alarm_sink.persistence_manager,
        "persist_alarm",
        lambda d: captured.append(d) or True,
    )

    cq = MagicMock()
    cq.task_id = 7
    cq.step_id = 1
    cq.source_ip = "c"
    cq.append_alarm_record_with_gate.return_value = False  # 冷却窗口拦截

    alarm = Alarm(alarm_type=AlarmType.MOCK, alarm_level="low", alarm_message="m")
    alarm_sink.persist_alarms([alarm], cq=cq, mode="REALTIME")

    assert captured == []  # 被闸门挡下，不落库
