"""
测试 ClientQueues 告警 gate（固定冷却窗口）及 alarm_log 基本行为。
"""

from types import SimpleNamespace
from unittest.mock import patch

from app.services.client.queues import ClientQueues
from app.services.inference.data_models import ALARM_MODE_REALTIME, Detection, DetectionOutput
from app.services.inference.models import AlarmRecord


def _make_record(metric="BUBBLE", mode="REALTIME", stage="LEAK") -> AlarmRecord:
    return AlarmRecord(
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


# --- gate 测试 ---

def test_gate_first_pass_allowed():
    cq = _cq_with_task()
    assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True


def test_gate_second_within_5s_blocked():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True
    with patch("time.time", return_value=t0 + 4.9):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is False


def test_gate_allowed_after_window_expires():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True
    with patch("time.time", return_value=t0 + 5.0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True


def test_gate_blocked_alarm_does_not_renew_window():
    """被拦截的告警不续期：固定冷却窗口，窗口从上次通过时刻计算，与中途拦截无关。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True   # 通过，last=t0
    with patch("time.time", return_value=t0 + 4.0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is False  # 拦截，last 仍为 t0
    with patch("time.time", return_value=t0 + 5.0):
        # 窗口从 t0 计，t0+5 到期；若续期应到 t0+9 才通过，验证非续期行为
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True


def test_gate_different_mode_independent():
    """REALTIME 和 SETTLEMENT 相同 metric 互不干扰。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "SETTLEMENT") is True


def test_gate_different_metric_independent():
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True
        assert cq.try_pass_alarm_gate(1, "BENDING", "REALTIME") is True


def test_gate_reset_on_task_change():
    """set_task 切换任务后 gate 清空，同 key 重新允许通过。"""
    cq = ClientQueues(client_id="c1")
    t0 = 1000.0
    cq.set_task(SimpleNamespace(task_id=1))
    with patch("time.time", return_value=t0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is True
    with patch("time.time", return_value=t0 + 1.0):
        assert cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME") is False  # 仍在窗口内

    # 切换到新任务 → gate 清空
    cq.set_task(SimpleNamespace(task_id=2))
    with patch("time.time", return_value=t0 + 1.0):
        assert cq.try_pass_alarm_gate(2, "BUBBLE", "REALTIME") is True


# --- append_alarm_record 测试 ---

def test_append_alarm_record_assigns_seq():
    cq = _cq_with_task()
    cq.append_alarm_record(_make_record())
    cq.append_alarm_record(_make_record())
    alarms = cq.get_alarm_increment(since_seq=0)
    assert [a.seq for a in alarms] == [1, 2]


def test_append_alarm_record_no_dedup():
    """append_alarm_record 本身不做去重，每次调用都产生新条目。"""
    cq = _cq_with_task()
    for _ in range(3):
        cq.append_alarm_record(_make_record())
    assert len(cq.get_alarm_increment(since_seq=0)) == 3
    assert cq.get_alarm_max_seq() == 3


# --- gate + append 联动测试 ---

def test_gate_controls_alarm_log_entries():
    """调用方遵守 gate：5s 内同 key 只写入一条。"""
    cq = _cq_with_task()
    t0 = 1000.0
    with patch("time.time", return_value=t0):
        if cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME"):
            cq.append_alarm_record(_make_record())
    with patch("time.time", return_value=t0 + 2.0):
        if cq.try_pass_alarm_gate(1, "BUBBLE", "REALTIME"):  # blocked
            cq.append_alarm_record(_make_record())

    assert len(cq.get_alarm_increment(since_seq=0)) == 1


# --- signals_10s 不受影响 ---

def test_signals_10s_summary():
    cq = ClientQueues(client_id="c1")
    bubble_detection = Detection(
        bbox=[0, 0, 10, 10], confidence=0.8, class_id=0, class_name="bubble"
    )
    cq.push_detection(
        "bubble",
        DetectionOutput(
            detections=[bubble_detection], metadata={}, timestamp=10.0, success=True
        ),
    )
    summary = cq.get_signals_10s()
    assert summary["BUBBLE"]["active"] is True
    assert summary["BUBBLE"]["hit_count"] == 1
    assert "BENDING" not in summary
