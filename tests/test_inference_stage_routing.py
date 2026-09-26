import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.inference.config import FALLBACK_STAGE
from app.services.inference.online.manager import InferenceManager
from app.utils.exceptions import ValidationError


@pytest.fixture
def manager():
    m = InferenceManager.__new__(InferenceManager)
    m._actors = {}
    return m


# 主键 = step_id：current_step 直接作 stage 主键（恒等路由）。
# YAML 配了 "1"/"2"/"3"/"MOCK"；其中 "3" 的 detector 全部加载失败 → 不在 active 集合。
_STAGE_CONFIGS = {"1": {}, "2": {}, "MOCK": {}}
_YAML = SimpleNamespace(list_stages=lambda: ["1", "2", "3", "MOCK"])


def _routing(manager):
    return (
        patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS),
        patch("app.services.inference.config.load_stage_config", return_value=_YAML),
    )


@pytest.mark.parametrize("step,expected_stage", [
    ("1", "1"),        # 已配 step → 恒等
    (2, "2"),          # int 与 str 同键
    ("3", "MOCK"),     # 配了但 detector 加载失败 → 推理失败兜底 MOCK
])
def test_resolve_stage_routes(manager, step, expected_stage):
    # stage 解析上移为公有 resolve_stage（供 RunController 建 CQ 前调用）。
    p_active, p_yaml = _routing(manager)
    with p_active, p_yaml:
        assert manager.resolve_stage(step) == expected_stage


@pytest.mark.parametrize("step", [99, "测漏", ""])
def test_resolve_stage_unconfigured_rejected(manager, step):
    """YAML 未定义的 step 是参数错误：抛 ValidationError（400），不兜底 MOCK。"""
    p_active, p_yaml = _routing(manager)
    with p_active, p_yaml, pytest.raises(ValidationError):
        manager.resolve_stage(step)


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
    with patch("app.services.inference.online.manager.client_manager") as cm, \
         patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS):
        assert manager.start_workflow(cq) is True

    cm.set.assert_not_called()   # 注册职责已上移 RunController，本方法不再 set


# ── 启动不变式：兜底 stage 必须 active ──────────────────────────────
#
# resolve_stage 把 detector 加载失败的 stage 路由到 FALLBACK_STAGE，而 dispatcher 只提交 active
# （有 detector）stage 的帧。若兜底 stage 被配掉 detector，启动仍会"成功"（只 INFO 一行
# Skipped），但此后兜底的 run 都取帧后无人消费 → 静默 0 推理。故须 fail-fast。
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
        ["1", "2", FALLBACK_STAGE], {"1": [object()], FALLBACK_STAGE: [object()]},  # "2" 加载失败
    )
    with p_cfg, p_fac:
        configs = m._get_stage_configs()
        # 不变式的实质：resolve_stage 的兜底目标必须落在 active 集合内
        assert FALLBACK_STAGE in configs
        assert m.resolve_stage("2") == FALLBACK_STAGE


def test_real_manager_init_invariants_and_stop_workflow_smoke():
    """真实构造 InferenceManager，守卫 __init__ 必设属性 + stop_workflow 空跑。

    其余测试均 mock/__new__ 绕过真构造，无法发现 __init__ 漏设属性（如 _actors）——
    本用例真构造一次兜底（权重仍惰性、无线程）。
    """
    m = InferenceManager()
    assert m._actors == {}                          # 漏设 → stop_workflow 会 AttributeError
    # 已摘除落盘服务引用（inference 不持 persistence / recording，落盘经 run_control 编排）
    assert not hasattr(m, "persistence_manager")
    assert not hasattr(m, "recording_service")
    assert not hasattr(m, "_client_lifecycle_lock")  # 互斥上移 RunController.lock_for
    # 无 actor、检测结果无残余 → 返回空 settlement、不抛
    cq = MagicMock()
    cq.task_id = 999
    cq.step_id = None
    assert m.stop_workflow(cq) == []
