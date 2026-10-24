"""run 状态机 + 写门 + close() —— 迟到写入在写入时刻被拒（全部 topic 一并验）。"""

from factories import make_alarm, make_session, make_frame, make_frame_detection
from app.services.session.core import SessionState
from app.services.session.topics import DETECTIONS, LATEST_RAW, PROCESSED, RAW, READY, RENDERED
from app.services.inference.online.topics import DETECTION_WINDOW, LATEST_DETECTION, LATEST_TEMPORAL


# 本地薄别名：保留原用例可读性，构造逻辑收敛在 factories。
def _det():
    return make_frame_detection(source="bubble", class_name="b")


# --- 转换：幂等、单调 ---

def test_state_starts_active():
    assert make_session().state is SessionState.ACTIVE
    assert make_session().is_active() is True


def test_transitions_idempotent_and_monotonic():
    cq = make_session()
    assert cq.seal() is True          # ACTIVE→DRAINING
    assert cq.seal() is False         # 幂等
    assert cq.state is SessionState.DRAINING
    cq.close()
    assert cq.state is SessionState.CLOSED
    assert cq.seal() is False         # 单调，不回退
    cq.close()                               # 幂等
    assert cq.state is SessionState.CLOSED


# --- 写门：生产者写在 DRAINING/CLOSED 被拒 ---

def test_frame_and_result_writes_blocked_when_not_active():
    cq = make_session()
    cq.seal()
    assert cq.channel(READY).push(make_frame()) is False
    assert cq.channel(RAW).push(make_frame()) is False
    assert cq.channel(LATEST_RAW).set(make_frame()) is False
    assert cq.channel(PROCESSED).push(make_frame()) is False
    assert cq.channel(DETECTIONS).push(_det()) is False
    cq.channel(DETECTION_WINDOW).push(_det())
    assert cq.channel(DETECTION_WINDOW).snapshot() == []
    assert cq.channel(LATEST_DETECTION).set(make_frame_detection()) is False
    assert cq.channel(LATEST_DETECTION).get() is None


# --- settlement 非对称：DRAINING 放行、CLOSED 拒 ---

def test_settlement_alarm_allowed_in_draining_rejected_when_closed(monkeypatch):
    from app.services.inference.online.temporal import alarm_sink

    persisted = []
    monkeypatch.setattr(alarm_sink.alarm_service, "persist_alarm", persisted.append)
    cq = make_session()
    cq.seal()
    alarm_sink.persist_alarms([make_alarm()], sess=cq, mode="SETTLEMENT")  # DRAINING 放行
    cq.close()
    alarm_sink.persist_alarms([make_alarm(metric="X")], sess=cq, mode="SETTLEMENT")  # CLOSED 拒
    assert len(persisted) == 1
    assert [a.seq for a in alarm_sink.ledger_for(cq).recent()] == [1]


# --- 清空写放行（拆除期清前端残帧/事件）---

def test_clear_through_writes_allowed_when_not_active():
    cq = make_session()
    cq.channel(RENDERED).set(make_frame())
    cq.channel(LATEST_TEMPORAL).set(["e1"])
    cq.seal()
    # 非空写被拒
    cq.channel(RENDERED).set(make_frame())
    cq.channel(LATEST_TEMPORAL).set(["e2"])
    # 清空写放行
    cq.channel(RENDERED).clear()
    cq.channel(LATEST_TEMPORAL).clear()
    assert cq.channel(RENDERED).get() is None
    assert cq.channel(LATEST_TEMPORAL).get() == []


# --- close 释放 payload、保留身份 ---

def test_close_releases_payload_keeps_identity():
    cq = make_session()
    cq.channel(DETECTION_WINDOW).push(_det())
    cq.channel(RAW).push(make_frame())
    cq.close()
    # payload 已释放
    assert cq.channel(DETECTION_WINDOW).snapshot() == []
    assert len(cq.channel(RAW)) == 0
    assert cq.channel(LATEST_DETECTION).get() is None
    # 身份小壳保留（供 fence/日志）
    assert cq.info.run.task_id == 1
    assert cq.info.run.step_id == 1
    assert cq.info.stage == "1"

