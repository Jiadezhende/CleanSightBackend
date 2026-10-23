"""迁移桥：start_run / stop_run 同步 session 的 open / seal / close，CQ 挂 `cq.sess`。"""

from unittest.mock import patch

import pytest

from factories import make_cq
from app.services.client.instance import client_service
from app.services.run_control.instance import run_control_service
from app.services.session.core import SessionState
from app.services.session.instance import session_service


@pytest.fixture
def tid():
    tid = 6161
    yield tid
    if client_service.has_client(tid):
        client_service.remove(tid, cleanup=False)
    client_service._task_locks.pop(tid, None)


def _patched():
    return (
        patch("app.services.run_control.service.inference_service"),
        patch("app.services.run_control.service.stream_service"),
        patch("app.services.run_control.service.recording_service"),
    )


def test_start_registers_session_and_stop_closes_it(tid):
    p_inf, p_stream, p_rec = _patched()
    with p_inf as inf, p_stream, p_rec:
        inf.resolve_stage.return_value = "1"
        inf.start_workflow.return_value = True
        inf.stop_workflow.return_value = []
        run_control_service.start_run(task_id=tid, current_step="1", rtsp_url="rtsp://x", source_ip="ip")

        cq = client_service.get(tid)
        sess = session_service.get(tid)
        assert cq.sess is sess and sess.is_active()
        assert sess.info.run == cq.run and sess.info.source_ip == "ip" and sess.info.stage == "1"
        assert cq.task_started_at == sess.info.started_at

        seen = {}
        inf.stop_workflow.side_effect = lambda _cq: seen.setdefault("state", sess.state) and []
        run_control_service.stop_run(tid, reason="test")

    assert seen["state"] is SessionState.DRAINING   # 封闸先于停 workflow
    assert not session_service.has(tid)
    assert sess.state is SessionState.CLOSED


def test_stop_run_expected_miss_spares_new_session(tid):
    old = make_cq(task_id=tid)
    new = make_cq(task_id=tid)
    client_service.set(tid, new)
    p_inf, p_stream, p_rec = _patched()
    with p_inf, p_stream, p_rec:
        result = run_control_service.stop_run(tid, reason="stale", expected=old)
    assert result["skipped"] is True
    assert new.sess.is_active() and old.sess.is_active()


def test_cq_state_mirrors_to_session():
    cq = make_cq()
    cq.to_draining()
    assert cq.sess.state is SessionState.DRAINING
    cq.close()
    assert cq.sess.state is SessionState.CLOSED
