"""T3 收尾：stop_run 封闸(DRAINING)置位 + HealthMonitor 路径对象身份 fence。

覆盖两条不变式：
1. 封闸先于落盘：stop_run 在停生产者/落盘之前先把 CQ 置 DRAINING，迟到写被门拒；拆完 CLOSED。
2. 对象身份 fence：HM「先决策后拿锁」窗口内槽位被 /start 换成新 run 时，stop_run(expected=旧cq)
   整段放弃，绝不误停/误清健康新 run。
"""

from unittest.mock import MagicMock, patch

import pytest

from app.services.client.manager import client_manager
from app.services.client.queues import ClientQueues, RunState
from app.services.run_control import run_controller


def _cq(source_ip="10.9.9.9", task_id=1, step="1"):
    return ClientQueues(
        task_id=task_id, current_step=step, source_ip=source_ip, stage=step,
    )


@pytest.fixture
def _clean_registry():
    """隔离真实单例状态：测试前后清掉被测 client 的槽位与锁。"""
    cid = "10.9.9.9"
    yield cid
    client_manager.remove(cid, cleanup=False) if client_manager.has_client(cid) else None
    client_manager._task_locks.pop(cid, None)


# --- 1. 封闸先于落盘 ---

def test_stop_run_drains_before_flush_then_closes(_clean_registry):
    cid = _clean_registry
    cq = _cq(cid)
    client_manager.set(cid, cq)

    seen_state = {}

    def capture_state(cq_arg):
        # 停 workflow 时刻 CQ 必须已 DRAINING（生产者写已封、settlement 仍放行）
        seen_state["at_flush"] = cq.get_state()
        return []  # 无 settlement

    with (
        patch("app.services.run_control.stream_service") as mock_stream,
        patch("app.services.run_control.inference_manager") as mock_inf,
        patch("app.services.run_control.persistence_manager"),
    ):
        mock_inf.stop_workflow.side_effect = capture_state
        result = run_controller.stop_run(cid, reason="test")

    assert seen_state["at_flush"] is RunState.DRAINING
    mock_stream.stop_stream.assert_called_once_with(cid)
    # 拆完：CQ 已 CLOSED（remove→clear→close），出表
    assert cq.get_state() is RunState.CLOSED
    assert not client_manager.has_client(cid)
    assert result["client_cleaned"] is True


# --- 2a. 身份 fence 命中放行（槽位仍是 expected） ---

def test_stop_run_expected_hit_tears_down(_clean_registry):
    cid = _clean_registry
    cq = _cq(cid)
    client_manager.set(cid, cq)

    with (
        patch("app.services.run_control.stream_service") as mock_stream,
        patch("app.services.run_control.inference_manager") as mock_inf,
        patch("app.services.run_control.persistence_manager"),
    ):
        result = run_controller.stop_run(cid, reason="hm", expected=cq)

    mock_stream.stop_stream.assert_called_once_with(cid)
    mock_inf.stop_workflow.assert_called_once_with(cq)
    assert cq.get_state() is RunState.CLOSED
    assert not client_manager.has_client(cid)
    assert result.get("skipped") is not True


# --- 2b. 身份 fence 未命中：槽位已被换成新 run → 整段放弃 ---

def test_stop_run_expected_miss_skips_and_spares_new_run(_clean_registry):
    cid = _clean_registry
    cq_old = _cq(cid, task_id=1)
    cq_new = _cq(cid, task_id=2)
    client_manager.set(cid, cq_new)  # 槽位已是新 run（模拟 /start 抢占重启换槽）

    with (
        patch("app.services.run_control.stream_service") as mock_stream,
        patch("app.services.run_control.inference_manager") as mock_inf,
        patch("app.services.run_control.persistence_manager") as mock_persist,
    ):
        # HM 过期决策：拿着旧 cq 来拆，但槽位已换新
        result = run_controller.stop_run(cid, reason="hm-stale", expected=cq_old)

    assert result["skipped"] is True
    # 新 run 毫发无伤：未停 decoder、未落盘、仍在表、仍 ACTIVE
    mock_stream.stop_stream.assert_not_called()
    mock_inf.stop_workflow.assert_not_called()
    mock_persist.flush_residual_segments.assert_not_called()
    assert client_manager.get(cid) is cq_new
    assert cq_new.get_state() is RunState.ACTIVE


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
