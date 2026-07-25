"""守卫:start_run 中途失败必须回滚——CQ 不能泄漏在注册表。

背景:CQ 的 set/remove 均归 RunController(与 stop_run 对称)。start_run 在 set 注册 CQ 后,
把 persistence.start_run / start_workflow / start_stream 包进 try;任一步抛异常即调 stop_run
对称回滚(client_manager.remove 注销 CQ)。本用例锁死"失败→注销、不留泄漏"。
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.client.manager import client_manager
from app.services.run_control import run_controller


def test_start_run_rolls_back_cq_on_workflow_failure():
    task_id = 4242
    # 前置清场:确保注册表无该 task
    if client_manager.has_client(task_id):
        client_manager.remove(task_id, cleanup=False)

    mock_cq = MagicMock()
    mock_cq.task_id = task_id
    mock_cq.step_id = 0
    mock_cq.stage = "0"
    mock_cq.source_ip = "10.9.9.9"

    with (
        patch("app.services.run_control.ClientQueues", return_value=mock_cq),
        patch("app.services.run_control.inference_manager") as mock_inf,
        patch("app.services.run_control.stream_service"),
        patch("app.services.run_control.persistence_manager"),
        patch("app.services.run_control.alarm_sink"),
    ):
        mock_inf.resolve_stage.return_value = "0"
        # start_workflow 抛异常(如 actor.start 失败)——此时 CQ 已被 set 注册
        mock_inf.start_workflow.side_effect = RuntimeError("boom: actor.start failed")
        mock_inf.stop_workflow.return_value = []

        with pytest.raises(RuntimeError, match="boom"):
            run_controller.start_run(task_id=task_id, current_step="0", rtsp_url="rtsp://x")

    # 回滚已注销:CQ 不泄漏在注册表(修复前:set 在 start_workflow 内,失败后无人 remove → 泄漏)
    assert not client_manager.has_client(task_id)

    # 清理本用例建的 per-task 锁,避免跨用例残留
    client_manager._task_locks.pop(task_id, None)
