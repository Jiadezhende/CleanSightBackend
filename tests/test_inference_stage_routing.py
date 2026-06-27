import threading

import pytest
from unittest.mock import MagicMock, patch

from app.models.task import Task
from app.services.inference.core.manager import InferenceManager


@pytest.fixture
def manager():
    m = InferenceManager.__new__(InferenceManager)
    m._actors = {}
    m._client_lifecycle_lock = threading.Lock()
    return m


def _make_task(step: str) -> Task:
    return Task(
        task_id=1,
        current_step=step,
        status="running",
        updated_at=0,
        fully_submerged=False,
        bending=False,
        bubble_detected=False,
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
    cq = MagicMock()
    with patch("app.services.inference.core.manager.client_manager") as cm, \
         patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS):
        cm.get_client.return_value = cq
        manager.set_task("client_1", _make_task(step))
    cq.set_stage.assert_called_once_with(expected_stage)


def test_set_task_none_skips_stage(manager):
    cq = MagicMock()
    with patch("app.services.inference.core.manager.client_manager") as cm, \
         patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS):
        cm.get_client.return_value = cq
        manager.set_task("client_1", None)
    cq.set_stage.assert_not_called()
