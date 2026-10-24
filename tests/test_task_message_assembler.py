"""前端消息装配层（router 侧）守卫。

signals_10s 的「流名→metric 映射 + 空模板」与告警序列化已从运行态存储下沉到
app.routers.task 的装配函数；帧窗汇总（_summarize_window）也在 router。
"""

from unittest.mock import MagicMock

from factories import make_alarm, make_frame_detection

import app.services.inference.online.naming as naming
from app.types.run import RunIdentity
from app.types.alarm import AlarmMetric
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

    sess = MagicMock()
    sess.info.run = RunIdentity(7, 1, 1)
    ledger = MagicMock()
    ledger.snapshot.return_value = ([alarm], 2)
    from app.services.inference.online.temporal import alarm_sink
    monkeypatch.setattr(alarm_sink, "ledger_for", lambda sess: ledger)
    sess.channel.return_value.snapshot.return_value = [
        make_frame_detection(source="bubble", n=1, ts=1.0)
    ]

    msg = _build_task_alarm_message(sess, since_seq=1)

    assert msg["task_id"] == 7
    assert msg["max_seq"] == 2
    assert msg["signals_10s"]["BUBBLE"]["active"] is True
    assert msg["alarms"][0]["seq"] == 2
    assert msg["alarms"][0]["metric"] == AlarmMetric.BUBBLE
    assert msg["alarms"][0]["ts"] == 1
    # 走原子入口，since_seq 透传
    ledger.snapshot.assert_called_once_with(1)
