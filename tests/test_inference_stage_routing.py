import threading

import pytest
from unittest.mock import MagicMock, patch

from app.domain.task import CleaningTask
from app.services.inference.manager import InferenceManager


@pytest.fixture
def manager():
    m = InferenceManager.__new__(InferenceManager)
    m._actors = {}
    m._client_lifecycle_lock = threading.Lock()
    return m


def _make_task(step: str) -> CleaningTask:
    return CleaningTask(
        task_id=1,
        current_step=step,
        status="running",
    )


# 主键 = step_id：current_step 直接作 stage 主键（恒等路由），未配的 step 回退 MOCK。
# stage_configs 含 "1"/"2"/"MOCK" 三个已配阶段。
_STAGE_CONFIGS = {"1": {}, "2": {}, "MOCK": {}}


@pytest.mark.parametrize("step,expected_stage", [
    ("1", "1"),        # 已配 step → 恒等
    ("2", "2"),        # 已配 step → 恒等
    ("测漏", "MOCK"),  # 未配 step → 兜底 MOCK
    ("", "MOCK"),      # 空 step → 兜底 MOCK
])
def test_set_task_routes_stage(manager, step, expected_stage):
    # 身份不可变：set_task 建**新** CQ（stage 构造注入）并换槽。断言构造时的 stage kwarg。
    with patch("app.services.inference.manager.client_manager") as cm, \
         patch("app.services.inference.manager.ClientQueues") as CQ, \
         patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS), \
         patch("app.services.client.config.get_client_config") as gcc:
        cm.get.return_value = None            # 无旧 run
        gcc.return_value.cq_kwargs.return_value = {}
        CQ.return_value.step_id = None        # 跳过 open_fresh
        manager.set_task("client_1", _make_task(step))

    assert CQ.call_args.kwargs["stage"] == expected_stage
    cm.set.assert_called_once()               # 新 CQ 换槽


def test_set_task_none_skips_run(manager):
    # task=None：仅停旧 actor，不建 CQ、不换槽（历史语义）。
    with patch("app.services.inference.manager.client_manager") as cm, \
         patch("app.services.inference.manager.ClientQueues") as CQ, \
         patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS):
        cm.get.return_value = None
        manager.set_task("client_1", None)

    CQ.assert_not_called()
    cm.set.assert_not_called()
