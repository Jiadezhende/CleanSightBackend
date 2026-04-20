import pytest
from unittest.mock import MagicMock, patch

from app.models.task import Task
from app.services.inference.core.manager import InferenceManager


@pytest.fixture
def manager():
    m = InferenceManager.__new__(InferenceManager)
    m._actors = {}
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


@pytest.mark.parametrize("step,expected_stage", [
    ("1", "LEAK"),
    ("2", "CLEAN"),
    ("测漏", "MOCK"),
    ("", "MOCK"),
])
def test_set_task_routes_stage(manager, step, expected_stage):
    cq = MagicMock()
    with patch("app.services.inference.core.manager.client_manager") as cm, \
         patch.object(manager, "_get_stage_configs", return_value={}):
        cm.get_client.return_value = cq
        manager.set_task("client_1", _make_task(step))
    cq.set_stage.assert_called_once_with(expected_stage)


def test_set_task_none_skips_stage(manager):
    cq = MagicMock()
    with patch("app.services.inference.core.manager.client_manager") as cm, \
         patch.object(manager, "_get_stage_configs", return_value={}):
        cm.get_client.return_value = cq
        manager.set_task("client_1", None)
    cq.set_stage.assert_not_called()
