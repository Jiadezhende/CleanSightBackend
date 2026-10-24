"""守卫：进程停机时 run_control 逐个拆掉在跑的 run，残段 / 结算告警交出去。"""

from unittest.mock import patch

from factories import open_session
from app.services.run_control.instance import run_control_service
from app.services.session.instance import session_service


def test_shutdown_stops_every_run():
    sessions = {tid: open_session(task_id=tid, step_id=0, source_ip="10.9.9.9") for tid in (5151, 5152)}

    with (
        patch("app.services.run_control.service.stream_service") as stream,
        patch("app.services.run_control.service.inference_service") as inference,
        patch("app.services.run_control.service.recording_service") as recording,
        patch("app.services.run_control.service.alarm_sink") as alarm_sink,
    ):
        inference.stop_workflow.return_value = ["settlement"]
        run_control_service.shutdown()

    for tid, sess in sessions.items():
        stream.stop_stream.assert_any_call(tid)
        inference.stop_workflow.assert_any_call(sess)
        recording.flush_residual.assert_any_call(sess)
        assert not session_service.has(tid)
    assert alarm_sink.persist_alarms.call_count == len(sessions)
    stream.shutdown.assert_not_called()   # stream 自身的收尾归 stream.lifespan


def test_stop_run_flushes_residual_even_if_settlement_fails():
    """结算告警上报抛错：残段仍须落盘、session 仍须注销，错误记进 result。"""
    tid = 5153
    sess = open_session(task_id=tid, step_id=0, source_ip="10.9.9.9")

    with (
        patch("app.services.run_control.service.stream_service"),
        patch("app.services.run_control.service.inference_service") as inference,
        patch("app.services.run_control.service.recording_service") as recording,
        patch("app.services.run_control.service.alarm_sink") as alarm_sink,
    ):
        inference.stop_workflow.return_value = ["settlement"]
        alarm_sink.persist_alarms.side_effect = RuntimeError("alarm queue down")
        result = run_control_service.stop_run(tid, reason="test")

    recording.flush_residual.assert_called_once_with(sess)
    assert result["data_flushed"] is True
    assert any(e.startswith("settlement:") for e in result["errors"])
    assert not session_service.has(tid)
