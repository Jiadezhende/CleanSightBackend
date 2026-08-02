import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.inference.config import FALLBACK_STAGE
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


# ── 启动不变式：兜底 stage 必须 active ──────────────────────────────
#
# resolve_stage 把未知 step_id 一律路由到 FALLBACK_STAGE，而 dispatcher 只提交 active
# （有 detector）stage 的帧。若兜底 stage 被配掉 detector，启动仍会"成功"（只 INFO 一行
# Skipped），但此后每个未知 step_id 的 run 都取帧后无人消费 → 静默 0 推理。故须 fail-fast。
# 这里在 config/factory 这层 seam 上测，不碰真权重加载（I/O 边界集成-only）。


def _patched_get_stage_configs(stage_names, detectors_by_stage):
    """注入假 config/factory 跑真实 _get_stage_configs，返回 (manager, ctx管理器对)。"""
    m = InferenceManager.__new__(InferenceManager)
    m._stage_configs = None
    fake_config = SimpleNamespace(list_stages=lambda: list(stage_names), batch_size=4)
    fake_factory = MagicMock()
    fake_factory.create_detectors_for_stage.side_effect = (
        lambda s: list(detectors_by_stage.get(s, []))
    )
    fake_factory.create_operators_for_stage.side_effect = lambda s: []
    return m, (
        patch("app.services.inference.config.load_stage_config", return_value=fake_config),
        patch("app.services.inference.stage_factory.StageFactory", return_value=fake_factory),
    )


def test_fallback_stage_without_detector_fails_fast():
    """兜底 stage 无 detector → 启动即抛，不放行成静默黑洞。"""
    m, (p_cfg, p_fac) = _patched_get_stage_configs(
        ["1", FALLBACK_STAGE], {"1": [object()]},  # 兜底 stage 被配掉 detector
    )
    with p_cfg, p_fac, pytest.raises(RuntimeError, match=FALLBACK_STAGE):
        m._get_stage_configs()


def test_fallback_stage_with_detector_passes():
    """兜底 stage 有 detector → 正常放行，且它在 active 集合里（dispatcher 会消费它）。"""
    m, (p_cfg, p_fac) = _patched_get_stage_configs(
        ["1", FALLBACK_STAGE], {"1": [object()], FALLBACK_STAGE: [object()]},
    )
    with p_cfg, p_fac:
        configs = m._get_stage_configs()
    # 不变式的实质：resolve_stage 的兜底目标必须落在 active 集合内
    assert FALLBACK_STAGE in configs
    assert m.resolve_stage("未配的step") == FALLBACK_STAGE


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
