"""前端消息装配层（router 侧）守卫。

signals_10s 的「流名→metric 映射 + 空模板」与告警序列化已从 ClientQueues 下沉到
app.routers.task 的装配函数；CQ 只出纯数据（get_slide_window_summary / get_alarm_snapshot）。
"""

from unittest.mock import MagicMock

from factories import make_alarm

import app.services.inference.naming as naming
from app.domain.alarm import AlarmMetric
from app.routers.task import _build_signals_10s, _build_task_alarm_message


def _patch_metric_map(monkeypatch, mapping):
    monkeypatch.setattr(naming, "get_task_metric_map", lambda: mapping)


def test_build_signals_10s_empty_template(monkeypatch):
    _patch_metric_map(monkeypatch, {"bubble": AlarmMetric.BUBBLE, "bending": AlarmMetric.BENDING})
    out = _build_signals_10s({})
    # 全量 metric 空模板，即使无实时数据也返回一致 schema
    assert out["BUBBLE"] == {"active": False, "hit_count": 0, "max_conf": 0.0}
    assert out["BENDING"] == {"active": False, "hit_count": 0, "max_conf": 0.0}
    assert set(out) == {"BUBBLE", "BENDING"}


def test_build_signals_10s_overlays_live_and_ignores_unmapped(monkeypatch):
    _patch_metric_map(monkeypatch, {"bubble": AlarmMetric.BUBBLE, "bending": AlarmMetric.BENDING})
    out = _build_signals_10s({
        "bubble": {"active": True, "hit_count": 3, "max_conf": 0.9},
        "ghost": {"active": True, "hit_count": 1, "max_conf": 0.5},  # 无 metric 映射→忽略
    })
    assert out["BUBBLE"] == {"active": True, "hit_count": 3, "max_conf": 0.9}
    assert out["BENDING"]["active"] is False  # 未覆盖仍空模板
    assert set(out) == {"BUBBLE", "BENDING"}   # ghost 被丢弃


def test_build_task_alarm_message_assembles_via_atomic_snapshot(monkeypatch):
    _patch_metric_map(monkeypatch, {"bubble": AlarmMetric.BUBBLE})

    alarm = make_alarm(metric=AlarmMetric.BUBBLE, mode="REALTIME", seq=2, timestamp=1.0)

    cq = MagicMock()
    cq.task_id = 7
    cq.get_alarm_snapshot.return_value = ([alarm], 2)
    cq.get_slide_window_summary.return_value = {
        "bubble": {"active": True, "hit_count": 1, "max_conf": 0.8}
    }

    msg = _build_task_alarm_message(cq, since_seq=1)

    assert msg["task_id"] == 7
    assert msg["max_seq"] == 2
    assert msg["signals_10s"]["BUBBLE"]["active"] is True
    assert msg["alarms"][0]["seq"] == 2
    assert msg["alarms"][0]["metric"] == AlarmMetric.BUBBLE
    assert msg["alarms"][0]["ts"] == 1
    # 走原子入口，since_seq 透传
    cq.get_alarm_snapshot.assert_called_once_with(1)
