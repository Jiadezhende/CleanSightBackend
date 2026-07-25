import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.inference.manager import InferenceManager


@pytest.fixture
def manager():
    m = InferenceManager.__new__(InferenceManager)
    m._actors = {}
    return m


# 主键 = step_id：current_step 直接作 stage 主键（恒等路由），未配的 step 回退 MOCK。
# stage_configs 含 "1"/"2"/"MOCK" 三个已配阶段。
_STAGE_CONFIGS = {"1": {}, "2": {}, "MOCK": {}}


@pytest.mark.parametrize("step,expected_stage", [
    ("1", "1"),        # 已配 step → 恒等
    ("2", "2"),        # 已配 step → 恒等
    ("测漏", "MOCK"),  # 未配 step → 兜底 MOCK
    ("", "MOCK"),      # 空 step → 兜底 MOCK
])
def test_resolve_stage_routes(manager, step, expected_stage):
    # stage 解析上移为公有 resolve_stage（供 RunController 建 CQ 前调用）。
    with patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS):
        assert manager.resolve_stage(step) == expected_stage


def _fake_cq(task_id=1, stage="1", step_id=None):
    cq = MagicMock()
    cq.task_id = task_id
    cq.stage = stage
    cq.step_id = step_id
    return cq


def test_start_workflow_no_set_no_actor(manager):
    # start_workflow(cq) 不再碰注册表（set/remove 均归 RunController，与 stop_run 对称）。
    # 无 operator_specs → 不建 actor。CQ 假定已由 RunController 注册。
    cq = _fake_cq(task_id=7, stage="MOCK", step_id=None)
    with patch("app.services.inference.manager.client_manager") as cm, \
         patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS):
        assert manager.start_workflow(cq) is True

    cm.set.assert_not_called()   # 注册职责已上移 RunController，本方法不再 set


def test_real_manager_init_invariants_and_stop_workflow_smoke():
    """真实构造 InferenceManager，守卫 __init__ 必设属性 + stop_workflow 空跑。

    其余测试均 mock/__new__ 绕过真构造，无法发现 __init__ 漏设属性（如 _actors）——
    本用例真构造一次兜底（权重仍惰性、无线程）。
    """
    m = InferenceManager()
    assert m._actors == {}                          # 漏设 → stop_workflow 会 AttributeError
    assert not hasattr(m, "persistence_manager")    # 已摘除持久化引用
    assert not hasattr(m, "_client_lifecycle_lock")  # 互斥上移 RunController.lock_for
    # 无 actor、feature close 空跑 → 返回空 settlement、不抛
    cq = MagicMock()
    cq.task_id = 999
    cq.step_id = None
    assert m.stop_workflow(cq) == []
