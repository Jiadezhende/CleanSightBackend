"""守卫：进程停机时 run_control 逐个拆掉在跑的 run，残段 / 结算告警交出去。"""

from unittest.mock import MagicMock, patch

from app.services.client.instance import client_service
from app.services.run_control.instance import run_control_service
from app.types.run import RunIdentity


def _fake_cq(task_id: int) -> MagicMock:
    cq = MagicMock()
    cq.run = RunIdentity(task_id, 0, 1)
    cq.source_ip = "10.9.9.9"
    return cq


def test_shutdown_stops_every_run():
    cqs = {tid: _fake_cq(tid) for tid in (5151, 5152)}
    for tid, cq in cqs.items():
        client_service.set(tid, cq)

    with (
        patch("app.services.run_control.service.stream_service") as stream,
        patch("app.services.run_control.service.inference_service") as inference,
        patch("app.services.run_control.service.recording_service") as recording,
        patch("app.services.run_control.service.alarm_sink") as alarm_sink,
    ):
        inference.stop_workflow.return_value = ["settlement"]
        run_control_service.shutdown()

    try:
        for tid, cq in cqs.items():
            stream.stop_stream.assert_any_call(tid)
            inference.stop_workflow.assert_any_call(cq)
            recording.flush_residual.assert_any_call(cq)
            assert not client_service.has_client(tid)
        assert alarm_sink.persist_alarms.call_count == len(cqs)
        stream.shutdown.assert_not_called()   # stream 自身的收尾归 stream.lifespan
    finally:
        for tid in cqs:
            client_service._task_locks.pop(tid, None)
