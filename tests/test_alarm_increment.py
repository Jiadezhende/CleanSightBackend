"""
测试每 run 告警账本 AlarmLedger 的 gate（固定冷却窗口）及日志基本行为。

闸门与入日志是单一原子方法 record：
返回 True = 通过闸门并已记录（赋 seq），False = 被冷却窗口拦截、未记录。
"""

from unittest.mock import patch

from factories import make_alarm, make_frame_detection
from app.services.inference.online.temporal.alarm_ledger import AlarmLedger


# --- gate 测试（经 record 返回值验证）---

def test_gate_blocked_alarm_does_not_renew_window():
    """固定 5s 冷却窗口：首条放行、窗口内拦截、到期（闭边界）放行；被拦截的告警不续期。"""
    ledger = AlarmLedger()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert ledger.record(make_alarm(), "REALTIME") is True   # 通过，last=t0
    with patch("time.time", return_value=t0 + 4.9):
        assert ledger.record(make_alarm(), "REALTIME") is False  # 拦截，last 仍为 t0
    with patch("time.time", return_value=t0 + 5.0):
        # 窗口从 t0 计，t0+5 到期；若续期应到 t0+9.9 才通过，验证非续期行为
        assert ledger.record(make_alarm(), "REALTIME") is True


def test_gate_different_mode_independent():
    """REALTIME 和 SETTLEMENT 相同 metric 互不干扰。"""
    ledger = AlarmLedger()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert ledger.record(make_alarm(), "REALTIME") is True
        assert ledger.record(make_alarm(), "SETTLEMENT") is True


def test_gate_different_metric_independent():
    ledger = AlarmLedger()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert ledger.record(make_alarm(metric="BUBBLE"), "REALTIME") is True
        assert ledger.record(make_alarm(metric="BENDING"), "REALTIME") is True


# --- 入日志 / seq 测试 ---

def test_pass_assigns_incrementing_seq():
    """不同 metric 各自通过闸门，依次赋 seq。"""
    ledger = AlarmLedger()
    ledger.record(make_alarm(metric="BUBBLE"), "REALTIME")
    ledger.record(make_alarm(metric="BENDING"), "REALTIME")
    alarms, _ = ledger.snapshot(since_seq=0)
    assert [a.seq for a in alarms] == [1, 2]


def test_blocked_alarm_not_recorded():
    """被闸门拦截的告警不写入日志、不占 seq。"""
    ledger = AlarmLedger()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert ledger.record(make_alarm(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 2.0):
        assert ledger.record(make_alarm(), "REALTIME") is False  # blocked

    alarms, max_seq = ledger.snapshot(since_seq=0)
    assert len(alarms) == 1
    assert max_seq == 1


# --- 滑窗汇总（纯访问器，按流名键；metric 映射在 router 装配层测） ---

def test_window_summary():
    from app.routers.task import _summarize_window

    summary = _summarize_window([
        make_frame_detection(source="bubble", n=1, class_name="bubble", ts=-0.5),  # 10s 之外
        make_frame_detection(source="bubble", n=1, class_name="bubble", ts=10.0),
    ])
    assert summary["bubble"]["active"] is True
    assert summary["bubble"]["hit_count"] == 1
    assert "bending" not in summary  # 未推过的流不出现
