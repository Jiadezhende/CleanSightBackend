import pytest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from app.services.inference.online.manager import InferenceManager
from app.utils.exceptions import ValidationError


@pytest.fixture
def manager():
    m = InferenceManager.__new__(InferenceManager)
    m._actors = {}
    return m


# 主键 = step_id：current_step 直接作 stage 主键（恒等路由），无兜底 stage。
# YAML 配了 "1"/"2"/"3"；其中 "3" 没配 detector（无在线检测）→ 不在 active 集合。
_STAGE_CONFIGS = {"1": {}, "2": {}}
_YAML = SimpleNamespace(list_stages=lambda: ["1", "2", "3"])


def _routing(manager):
    return (
        patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS),
        patch("app.services.inference.config.load_stage_config", return_value=_YAML),
    )


@pytest.mark.parametrize("step,expected_stage", [
    ("1", "1"),        # 已配 step → 恒等
    (2, "2"),          # int 与 str 同键
])
def test_resolve_stage_routes(manager, step, expected_stage):
    # stage 解析上移为公有 resolve_stage（供 RunController 建 CQ 前调用）。
    p_active, p_yaml = _routing(manager)
    with p_active, p_yaml:
        assert manager.resolve_stage(step) == expected_stage


@pytest.mark.parametrize("step,reason", [
    (99, "未在推理配置中定义"),
    ("测漏", "未在推理配置中定义"),
    ("3", "未配置在线检测"),       # YAML 有、但没 detector
])
def test_resolve_stage_unrunnable_rejected(manager, step, reason):
    """未定义 / 无在线检测的 step 是参数错误：抛 ValidationError（400），无兜底 stage。"""
    p_active, p_yaml = _routing(manager)
    with p_active, p_yaml, pytest.raises(ValidationError, match=reason):
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
    cq = _fake_cq(task_id=7, stage="1", step_id=None)
    with patch("app.services.inference.online.manager.client_manager") as cm, \
         patch.object(manager, "_get_stage_configs", return_value=_STAGE_CONFIGS):
        assert manager.start_workflow(cq) is True

    cm.set.assert_not_called()   # 注册职责已上移 RunController，本方法不再 set


# ── 启动 fail-fast：detector 构造失败即启动失败 ─────────────────────────
#
# 构造不加载权重，能失败的只有配置错误；_get_stage_configs 不吞，包成 RuntimeError 冒到 lifespan。
# 这里在 config/factory 这层 seam 上测，不碰真权重加载（I/O 边界集成-only）。


def _patched_get_stage_configs(stage_names, detectors_by_stage):
    """注入假 config/factory 跑真实 _get_stage_configs，返回 (manager, ctx管理器对)。

    detectors_by_stage 的值为异常实例时，模拟该 stage 的 detector 构造失败。
    """
    m = InferenceManager.__new__(InferenceManager)
    m._stage_configs = None
    fake_config = SimpleNamespace(list_stages=lambda: list(stage_names), batch_size=4)

    def create_detectors(stage):
        got = detectors_by_stage.get(stage, [])
        if isinstance(got, Exception):
            raise got
        return list(got)

    fake_factory = MagicMock()
    fake_factory.create_detectors_for_stage.side_effect = create_detectors
    fake_factory.create_operators_for_stage.side_effect = lambda s: []
    return m, (
        patch("app.services.inference.config.load_stage_config", return_value=fake_config),
        patch("app.services.inference.stage_factory.StageFactory", return_value=fake_factory),
    )


def test_detector_construction_failure_fails_startup():
    """任一 stage 的 detector 构造失败 → _get_stage_configs 抛（后端启动失败），不降级成少一个 stage。"""
    m, (p_cfg, p_fac) = _patched_get_stage_configs(
        ["1", "2"], {"1": [object()], "2": RuntimeError("Stage '2' 创建 Detector 'x' 失败")},
    )
    with p_cfg, p_fac, pytest.raises(RuntimeError, match="Detector 'x'"):
        m._get_stage_configs()


def test_stage_without_detectors_inactive_not_fatal():
    """YAML 里没配 detector 的 stage 只是不生效（不是构造失败），启动照常。"""
    m, (p_cfg, p_fac) = _patched_get_stage_configs(["1", "3"], {"1": [object()]})
    with p_cfg, p_fac:
        assert list(m._get_stage_configs()) == ["1"]


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
