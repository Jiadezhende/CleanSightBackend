"""T2: CQ 状态机 + 写门 + close() —— 迟到写入在写入时刻被拒。"""

from types import SimpleNamespace

from factories import make_alarm, make_cq, make_frame, make_frame_detections
from app.services.client.queues import RunState


# 本地薄别名：保留原用例可读性，构造逻辑收敛在 factories。
def _det():
    return make_frame_detections(class_name="b")


# --- 转换：幂等、单调 ---

def test_state_starts_active():
    assert make_cq().get_state() is RunState.ACTIVE
    assert make_cq().is_active() is True


def test_transitions_idempotent_and_monotonic():
    cq = make_cq()
    assert cq.to_draining() is True          # ACTIVE→DRAINING
    assert cq.to_draining() is False         # 幂等
    assert cq.get_state() is RunState.DRAINING
    cq.close()
    assert cq.get_state() is RunState.CLOSED
    assert cq.to_draining() is False         # 单调，不回退
    cq.close()                               # 幂等
    assert cq.get_state() is RunState.CLOSED


# --- 写门：生产者写在 DRAINING/CLOSED 被拒 ---

def test_frame_and_result_writes_blocked_when_not_active():
    cq = make_cq()
    cq.to_draining()
    assert cq.append_ca_ready_with_throttle(make_frame()) is False
    assert cq.append_ca_raw(make_frame()) is False
    cq.append_ca_processed(make_frame())
    assert cq.get_ca_processed_length() == 0
    cq.push_detection("bubble", _det())
    assert cq.get_slide_window("bubble") == []
    cq.set_latest_inference(SimpleNamespace(detections={}, timestamp=1.0))
    assert cq.get_latest_inference() is None


# --- settlement 非对称：DRAINING 放行、CLOSED 拒 ---

def test_settlement_alarm_allowed_in_draining_rejected_when_closed():
    cq = make_cq()
    cq.to_draining()
    assert cq.append_alarm_record_with_gate(make_alarm(), "SETTLEMENT") is True  # DRAINING 放行
    cq.close()
    assert cq.append_alarm_record_with_gate(make_alarm(metric="X"), "SETTLEMENT") is False  # CLOSED 拒


# --- 清空写放行（拆除期清前端残帧/事件）---

def test_clear_through_writes_allowed_when_not_active():
    cq = make_cq()
    cq.set_latest_rendered(make_frame())
    cq.set_latest_temporal(["e1"])
    cq.to_draining()
    # 非空写被拒
    cq.set_latest_rendered(make_frame())
    cq.set_latest_temporal(["e2"])
    # 清空写放行
    cq.set_latest_rendered(None)
    cq.set_latest_temporal([])
    assert cq.get_latest_rendered() is None
    assert cq.get_latest_temporal() == []


# --- close 释放 payload、保留身份 ---

def test_close_releases_payload_keeps_identity():
    cq = make_cq()
    cq.push_detection("bubble", _det())
    cq.append_ca_raw(make_frame())
    cq.close()
    # payload 已释放
    assert cq.get_slide_window("bubble") == []
    assert cq.get_ca_raw_length() == 0
    assert cq.get_recent_alarms() == []
    assert cq.get_latest_inference() is None
    # 身份小壳保留（供 fence/日志）
    assert cq.task_id == 1
    assert cq.step_id == 1
    assert cq.stage == "1"


def test_clear_is_close_alias():
    """ClientManager.remove 走 clear() → 等价 close()（置 CLOSED + 释放 payload）。"""
    cq = make_cq()
    cq.clear()
    assert cq.get_state() is RunState.CLOSED


# --- 迟到写：持旧 CQ 句柄者在 close 后写被拒（不串台到新 run） ---

def test_late_write_to_closed_cq_rejected():
    old = make_cq()
    old.close()                              # 模拟旧 run 已拆除
    assert old.append_ca_ready_with_throttle(make_frame()) is False
    old.push_detection("bubble", _det())
    assert old.get_slide_window("bubble") == []
