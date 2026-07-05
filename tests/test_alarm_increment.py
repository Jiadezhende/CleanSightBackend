"""
测试 ClientQueues 告警 gate（固定冷却窗口）及 alarm_log 基本行为。

闸门与入日志已并入单一原子方法 append_alarm_record_with_gate：
返回 True = 通过闸门并已记录（赋 seq），False = 被冷却窗口拦截、未记录。
"""

from unittest.mock import patch

from factories import make_alarm, make_bare_cq, make_cq, make_frame_detections


def _cq_with_task():
    # 身份不可变：primitives 构造注入（一 CQ == 一 run）
    return make_cq(task_id=1, step_id=1, source_ip="c1")


# --- gate 测试（经 append_alarm_record_with_gate 返回值验证）---

def test_gate_first_pass_allowed():
    cq = _cq_with_task()
    assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True


def test_gate_second_within_5s_blocked():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 4.9):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is False


def test_gate_allowed_after_window_expires():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 5.0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True


def test_gate_blocked_alarm_does_not_renew_window():
    """被拦截的告警不续期：固定冷却窗口，窗口从上次通过时刻计算，与中途拦截无关。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True   # 通过，last=t0
    with patch("time.time", return_value=t0 + 4.0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is False  # 拦截，last 仍为 t0
    with patch("time.time", return_value=t0 + 5.0):
        # 窗口从 t0 计，t0+5 到期；若续期应到 t0+9 才通过，验证非续期行为
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True


def test_gate_different_mode_independent():
    """REALTIME 和 SETTLEMENT 相同 metric 互不干扰。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "SETTLEMENT") is True


def test_gate_different_metric_independent():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(metric="BUBBLE"), "REALTIME") is True
        assert cq.append_alarm_record_with_gate(1, make_alarm(metric="BENDING"), "REALTIME") is True


def test_gate_reset_on_task_change():
    """新 run = 新 CQ = fresh gate（一 CQ 一 run，身份不可变）：换 task 建新 CQ，同 key 重新允许。"""
    t0 = 1000.0
    cq1 = make_cq(task_id=1, step_id=1, source_ip="c1")
    with patch("time.time", return_value=t0):
        assert cq1.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 1.0):
        assert cq1.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is False  # 仍在窗口内

    # 切换任务 = 建**新** CQ（新 run），gate 天然为空
    cq2 = make_cq(task_id=2, step_id=1, source_ip="c1")
    with patch("time.time", return_value=t0 + 1.0):
        assert cq2.append_alarm_record_with_gate(2, make_alarm(), "REALTIME") is True


# --- 入日志 / seq 测试 ---

def test_pass_assigns_incrementing_seq():
    """不同 metric 各自通过闸门，依次赋 seq。"""
    cq = _cq_with_task()
    cq.append_alarm_record_with_gate(1, make_alarm(metric="BUBBLE"), "REALTIME")
    cq.append_alarm_record_with_gate(1, make_alarm(metric="BENDING"), "REALTIME")
    alarms = cq.get_alarm_increment(since_seq=0)
    assert [a.seq for a in alarms] == [1, 2]


def test_blocked_alarm_not_recorded():
    """被闸门拦截的告警不写入日志、不占 seq。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 2.0):
        assert cq.append_alarm_record_with_gate(1, make_alarm(), "REALTIME") is False  # blocked

    assert len(cq.get_alarm_increment(since_seq=0)) == 1
    assert cq.get_alarm_max_seq() == 1


# --- signals_10s 不受影响 ---

def test_signals_10s_summary():
    cq = make_bare_cq()
    cq.push_detection("bubble", make_frame_detections(n=1, class_name="bubble", ts=10.0))
    summary = cq.get_signals_10s()
    assert summary["BUBBLE"]["active"] is True
    assert summary["BUBBLE"]["hit_count"] == 1
    assert "BENDING" not in summary
