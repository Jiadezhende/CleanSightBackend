"""守卫:start_run 中途失败必须回滚——session 不能泄漏在注册表。

背景:session 的 open/close 均归 RunControlService(与 stop_run 对称)。start_run 在 open 注册 session 后,
把 start_workflow / start_stream 包进 try;任一步抛异常即调 stop_run(expected=sess) 对称回滚。
本用例锁死"失败→注销、不留泄漏"。
"""

from unittest.mock import patch

import pytest

from factories import open_session
from app.services.run_control.instance import run_control_service
from app.services.session.core import SessionState
from app.services.session.instance import session_service
from app.types.exceptions import ValidationError


def test_start_run_rolls_back_session_on_workflow_failure():
    task_id = 4242
    opened = []

    with (
        patch("app.services.run_control.service.inference_service") as mock_inf,
        patch("app.services.run_control.service.stream_service"),
        patch("app.services.run_control.service.recording_service"),
        patch("app.services.run_control.service.alarm_sink"),
    ):
        mock_inf.resolve_stage.return_value = "0"

        def boom(sess):
            opened.append(sess)
            raise RuntimeError("boom: actor.start failed")

        # start_workflow 抛异常(如 actor.start 失败)——此时 session 已注册
        mock_inf.start_workflow.side_effect = boom
        mock_inf.stop_workflow.return_value = []

        with pytest.raises(RuntimeError, match="boom"):
            run_control_service.start_run(task_id=task_id, current_step="0", rtsp_url="rtsp://x")

    # 回滚已注销:session 不泄漏在注册表，且已关闭
    assert not session_service.has(task_id)
    assert opened[0].state is SessionState.CLOSED


@pytest.mark.parametrize("current_step", ["未知阶段", "None"])
def test_start_run_non_numeric_step_rejected(current_step):
    """非数字 current_step 是参数错误：ValidationError（400），不解析 stage、不注册 session。"""
    with patch("app.services.run_control.service.inference_service") as mock_inf:
        with pytest.raises(ValidationError, match="不是数字"):
            run_control_service.start_run(task_id=4243, current_step=current_step, rtsp_url="rtsp://x")
    mock_inf.resolve_stage.assert_not_called()
    assert not session_service.has(4243)


def test_start_run_unconfigured_step_keeps_old_run():
    """未配置的 step 在动旧 run 之前就被拒：同 task 正在跑的 run 不被 stop。"""
    task_id = 4244
    old = open_session(task_id=task_id, step_id=1)
    with (
        patch("app.services.run_control.service.inference_service") as mock_inf,
        patch.object(run_control_service, "stop_run") as stop_run,
    ):
        mock_inf.resolve_stage.side_effect = ValidationError("step_id '99' 未在推理配置中定义")
        with pytest.raises(ValidationError):
            run_control_service.start_run(task_id=task_id, current_step="99", rtsp_url="rtsp://x")
    mock_inf.resolve_stage.assert_called_once_with(99)
    stop_run.assert_not_called()
    assert session_service.get(task_id) is old and old.is_active()
