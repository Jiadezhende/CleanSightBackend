"""
测试 ClientQueues 告警 gate（固定冷却窗口）及 alarm_log 基本行为。

闸门与入日志已并入单一原子方法 append_alarm_record_with_gate：
返回 True = 通过闸门并已记录（赋 seq），False = 被冷却窗口拦截、未记录。
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.services.client.queues import ClientQueues
from app.domain.alarm import Alarm
from app.domain.detection import Detection, FrameDetections


def _make_record(metric="BUBBLE", mode="REALTIME", stage="LEAK") -> Alarm:
    return Alarm(
        alarm_type="流程违规",
        alarm_level="high",
        alarm_message="test alarm",
        mode=mode,
        metric=metric,
        stage=stage,
    )


def _cq_with_task() -> ClientQueues:
    cq = ClientQueues(client_id="c1")
    cq.set_task(SimpleNamespace(task_id=1))
    return cq


# --- gate 测试（经 append_alarm_record_with_gate 返回值验证）---

def test_gate_first_pass_allowed():
    cq = _cq_with_task()
    assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True


def test_gate_second_within_5s_blocked():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 4.9):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is False


def test_gate_allowed_after_window_expires():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 5.0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True


def test_gate_blocked_alarm_does_not_renew_window():
    """被拦截的告警不续期：固定冷却窗口，窗口从上次通过时刻计算，与中途拦截无关。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True   # 通过，last=t0
    with patch("time.time", return_value=t0 + 4.0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is False  # 拦截，last 仍为 t0
    with patch("time.time", return_value=t0 + 5.0):
        # 窗口从 t0 计，t0+5 到期；若续期应到 t0+9 才通过，验证非续期行为
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True


def test_gate_different_mode_independent():
    """REALTIME 和 SETTLEMENT 相同 metric 互不干扰。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True
        assert cq.append_alarm_record_with_gate(1, _make_record(), "SETTLEMENT") is True


def test_gate_different_metric_independent():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, _make_record(metric="BUBBLE"), "REALTIME") is True
        assert cq.append_alarm_record_with_gate(1, _make_record(metric="BENDING"), "REALTIME") is True


def test_gate_reset_on_task_change():
    """set_task 切换任务后 gate 清空，同 key 重新允许通过。"""
    cq = ClientQueues(client_id="c1")
    t0 = 1000.0
    cq.set_task(SimpleNamespace(task_id=1))
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 1.0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is False  # 仍在窗口内

    # 切换到新任务 → gate 清空
    cq.set_task(SimpleNamespace(task_id=2))
    with patch("time.time", return_value=t0 + 1.0):
        assert cq.append_alarm_record_with_gate(2, _make_record(), "REALTIME") is True


# --- 入日志 / seq 测试 ---

def test_pass_assigns_incrementing_seq():
    """不同 metric 各自通过闸门，依次赋 seq。"""
    cq = _cq_with_task()
    cq.append_alarm_record_with_gate(1, _make_record(metric="BUBBLE"), "REALTIME")
    cq.append_alarm_record_with_gate(1, _make_record(metric="BENDING"), "REALTIME")
    alarms = cq.get_alarm_increment(since_seq=0)
    assert [a.seq for a in alarms] == [1, 2]


def test_blocked_alarm_not_recorded():
    """被闸门拦截的告警不写入日志、不占 seq。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is True
    with patch("time.time", return_value=t0 + 2.0):
        assert cq.append_alarm_record_with_gate(1, _make_record(), "REALTIME") is False  # blocked

    assert len(cq.get_alarm_increment(since_seq=0)) == 1
    assert cq.get_alarm_max_seq() == 1


# --- signals_10s 不受影响 ---

def test_signals_10s_summary():
    cq = ClientQueues(client_id="c1")
    bubble_detection = Detection(
        bbox=[0, 0, 10, 10], confidence=0.8, class_id=0, class_name="bubble"
    )
    cq.push_detection(
        "bubble",
        FrameDetections(
            detections=[bubble_detection], metadata={}, timestamp=10.0, success=True
        ),
    )
    summary = cq.get_signals_10s()
    assert summary["BUBBLE"]["active"] is True
    assert summary["BUBBLE"]["hit_count"] == 1
    assert "BENDING" not in summary
