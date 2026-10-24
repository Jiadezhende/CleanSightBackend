"""run_control × session：start_run 注册 session、stop_run 封闸 → 交出残帧 → 关 session。"""

from unittest.mock import patch

from factories import make_frame, make_frame_detection, open_session
from app.services.run_control.instance import run_control_service
from app.services.session.core import SessionState
from app.services.session.instance import session_service
from app.services.session.topics import DETECTIONS, PROCESSED, RAW

TID = 6161


def _patched():
    return (
        patch("app.services.run_control.service.inference_service"),
        patch("app.services.run_control.service.stream_service"),
        patch("app.services.run_control.service.recording_service"),
    )


def test_start_registers_session_and_stop_closes_it():
    p_inf, p_stream, p_rec = _patched()
    with p_inf as inf, p_stream, p_rec:
        inf.resolve_stage.return_value = "1"
        inf.start_workflow.return_value = True
        inf.stop_workflow.return_value = []
        run_control_service.start_run(task_id=TID, current_step="1", rtsp_url="rtsp://x", source_ip="ip")

        sess = session_service.get(TID)
        assert sess.is_active()
        assert (sess.info.run.task_id, sess.info.run.step_id) == (TID, 1)
        assert sess.info.source_ip == "ip" and sess.info.stage == "1" and sess.info.started_at > 0
        inf.start_workflow.assert_called_once_with(sess)

        seen = {}
        inf.stop_workflow.side_effect = lambda s: seen.setdefault("state", s.state) and []
        run_control_service.stop_run(TID, reason="test")

    assert seen["state"] is SessionState.DRAINING   # 封闸先于停 workflow
    assert not session_service.has(TID)
    assert sess.state is SessionState.CLOSED


def test_residual_frames_still_readable_at_flush_then_released():
    """残帧交接：flush_residual 时 session 已封闸但未关，RAW / PROCESSED / DETECTIONS 都还在；拆完才释放。"""
    sess = open_session(task_id=TID)
    sess.channel(RAW).push(make_frame(ts=1.0))
    sess.channel(PROCESSED).push(make_frame(ts=1.0))
    sess.channel(DETECTIONS).push(make_frame_detection(ts=1.0))

    seen = {}

    def flush(s):
        seen["state"] = s.state
        seen["sizes"] = (len(s.channel(RAW)), len(s.channel(PROCESSED)), len(s.channel(DETECTIONS)))

    p_inf, p_stream, p_rec = _patched()
    with p_inf as inf, p_stream, p_rec as rec:
        inf.stop_workflow.return_value = []
        rec.flush_residual.side_effect = flush
        run_control_service.stop_run(TID, reason="test")

    assert seen == {"state": SessionState.DRAINING, "sizes": (1, 1, 1)}
    assert sess.state is SessionState.CLOSED
    assert len(sess.channel(RAW)) == 0


def test_task_lock_is_owned_by_run_control_and_reentrant():
    """任务锁归 run_control：同 task 同一把、可重入（start_run 持锁内调 stop_run）。"""
    lk = run_control_service.lock_for(TID)
    assert run_control_service.lock_for(TID) is lk
    with lk:
        with run_control_service.lock_for(TID):
            pass
