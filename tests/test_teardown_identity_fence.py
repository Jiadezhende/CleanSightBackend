"""stop_run 封闸(DRAINING)置位 + HealthMonitor 路径对象身份 fence。

覆盖两条不变式：
1. 封闸先于落盘：stop_run 在停生产者/落盘之前先把 session 置 DRAINING，迟到写被门拒；拆完 CLOSED。
2. 对象身份 fence：HM「先决策后拿锁」窗口内注册项被 /start 换成新 run 时，stop_run(expected=旧 sess)
   整段放弃，绝不误停/误清健康新 run。
"""

from unittest.mock import patch

import pytest

from factories import make_session, open_session
from app.services.session.core import SessionState
from app.services.session.instance import session_service
from app.services.run_control.instance import run_control_service

TID = 1


# --- 1. 封闸先于落盘 ---

def test_stop_run_drains_before_flush_then_closes():
    sess = open_session(task_id=TID)

    seen_state = {}

    def capture_state(_sess):
        # 停 workflow 时刻 session 必须已 DRAINING（生产者写已封、settlement 仍放行）
        seen_state["at_flush"] = sess.state
        return []  # 无 settlement

    with (
        patch("app.services.run_control.service.stream_service") as mock_stream,
        patch("app.services.run_control.service.inference_service") as mock_inf,
        patch("app.services.run_control.service.recording_service"),
    ):
        mock_inf.stop_workflow.side_effect = capture_state
        result = run_control_service.stop_run(TID, reason="test")

    assert seen_state["at_flush"] is SessionState.DRAINING
    mock_stream.stop_stream.assert_called_once_with(TID)
    # 拆完：session 已 CLOSED，出表
    assert sess.state is SessionState.CLOSED
    assert not session_service.has(TID)
    assert result["client_cleaned"] is True


# --- 1b. 拆除侧的 recording flush：发生在 session 出注册表（close 释放帧）之前 ---

def test_stop_run_flushes_residual_while_session_still_registered():
    """`flush_residual` 必须在 close（释放帧）之前：反过来残帧已被释放。"""
    sess = open_session(task_id=TID)

    registered_at_flush = []

    with (
        patch("app.services.run_control.service.stream_service"),
        patch("app.services.run_control.service.inference_service") as mock_inf,
        patch("app.services.run_control.service.recording_service") as mock_recording,
    ):
        mock_inf.stop_workflow.return_value = []
        mock_recording.flush_residual.side_effect = (
            lambda s: registered_at_flush.append(session_service.get(TID) is s)
        )
        run_control_service.stop_run(TID, reason="test")

    mock_recording.flush_residual.assert_called_once_with(sess)
    assert registered_at_flush == [True]
    assert session_service.get(TID) is None


# --- 2a. 身份 fence 命中放行（注册项仍是 expected） ---

def test_stop_run_expected_hit_tears_down():
    sess = open_session(task_id=TID)

    with (
        patch("app.services.run_control.service.stream_service") as mock_stream,
        patch("app.services.run_control.service.inference_service") as mock_inf,
        patch("app.services.run_control.service.recording_service"),
    ):
        result = run_control_service.stop_run(TID, reason="hm", expected=sess)

    mock_stream.stop_stream.assert_called_once_with(TID)
    mock_inf.stop_workflow.assert_called_once_with(sess)
    assert sess.state is SessionState.CLOSED
    assert not session_service.has(TID)
    assert result.get("skipped") is not True


# --- 2b. 身份 fence 未命中：同 task_id 注册项已被换成新 run → 整段放弃 ---

def test_stop_run_expected_miss_skips_and_spares_new_run():
    old = make_session(task_id=TID)          # 同 task_id，不同对象（HM 捕获的旧 run）
    new = open_session(task_id=TID)          # 注册项已是新 run（模拟 /start 抢占重启）

    with (
        patch("app.services.run_control.service.stream_service") as mock_stream,
        patch("app.services.run_control.service.inference_service") as mock_inf,
        patch("app.services.run_control.service.recording_service") as mock_recording,
    ):
        # HM 过期决策：拿着旧 session 来拆，但注册项已换新
        result = run_control_service.stop_run(TID, reason="hm-stale", expected=old)

    assert result["skipped"] is True
    # 新 run 毫发无伤：未停 decoder、未落盘、仍在表、仍 ACTIVE
    mock_stream.stop_stream.assert_not_called()
    mock_inf.stop_workflow.assert_not_called()
    mock_recording.flush_residual.assert_not_called()
    assert session_service.get(TID) is new
    assert new.state is SessionState.ACTIVE and old.state is SessionState.ACTIVE


# --- 3. 孤儿 decoder：锁内复查注册表，有 run 即放弃 ---

def test_stop_orphan_decoder_spares_a_task_that_now_has_a_run():
    """HM 判孤儿后、拿锁前 /start 已注册新 run 与新 decoder → 放弃，不停新 run 的 decoder。"""
    open_session(task_id=TID)

    with patch("app.services.run_control.service.stream_service") as mock_stream:
        stopped = run_control_service.stop_orphan_decoder(TID)

    assert stopped is False
    mock_stream.stop_stream.assert_not_called()


def test_stop_orphan_decoder_stops_when_still_orphan():
    with patch("app.services.run_control.service.stream_service") as mock_stream:
        stopped = run_control_service.stop_orphan_decoder(TID)

    assert stopped is True
    mock_stream.stop_stream.assert_called_once_with(TID)


# --- 4. 重连 respawn：锁内核对注册项仍是 expected ---

def test_restart_stream_fence_miss_spares_new_run():
    """HM 进入重连后 /start 已换代 → 放弃 respawn，不停新 run 的 decoder、不把旧 URL 绑到新 run。"""
    old = make_session(task_id=TID)
    open_session(task_id=TID)

    with patch("app.services.run_control.service.stream_service") as mock_stream:
        ok = run_control_service.restart_stream(TID, "rtsp://old", expected=old)

    assert ok is False
    mock_stream.restart_stream.assert_not_called()


def test_restart_stream_fence_hit_restarts():
    sess = open_session(task_id=TID)

    with patch("app.services.run_control.service.stream_service") as mock_stream:
        mock_stream.restart_stream.return_value = True
        ok = run_control_service.restart_stream(TID, "rtsp://x", expected=sess)

    assert ok is True
    mock_stream.restart_stream.assert_called_once_with(task_id=TID, stream_url="rtsp://x")


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
