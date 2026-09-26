"""运行时命名注册表（YAML 驱动，启动时由 InferenceManager.start 灌入）。

- task_metric_map: stream_name(detector.name) → AlarmMetric（signals_10s 用）
- stage_alias:     stage 主键(step_id) → 可读别名（写告警 step_name + 可视化叠字）

均非数据契约，而是 inference 自有的运行时状态，故不进 app/domain，与被动契约分离。
"""

from typing import Dict

from app.domain.alarm import AlarmMetric

# YAML model name → AlarmMetric 映射，由 InferenceManager.start() 初始化
# 通过 get_task_metric_map() 访问，不要直接读取此变量
_TASK_METRIC_MAP: Dict[str, AlarmMetric] = {}


def _set_task_metric_map(mapping: Dict[str, AlarmMetric]) -> None:
    """由 InferenceManager.start() 调用一次，初始化映射。"""
    global _TASK_METRIC_MAP
    _TASK_METRIC_MAP.clear()
    _TASK_METRIC_MAP.update(mapping)


def get_task_metric_map() -> Dict[str, AlarmMetric]:
    """返回 task_name → AlarmMetric 映射（由 YAML model name 驱动）。

    若映射尚未初始化（如单元测试场景），自动从 YAML 懒加载一次。
    """
    if not _TASK_METRIC_MAP:
        from app.services.inference.config import load_stage_config
        from app.services.inference.stage_factory import StageFactory

        _set_task_metric_map(StageFactory(load_stage_config()).build_task_metric_map())
    return _TASK_METRIC_MAP


# stage 主键(step_id) → alias 映射，由 InferenceManager.start() 初始化
# alias 仅用于可读性出口（写告警 step_name + 可视化叠字）；功能性标识一律用主键
_STAGE_ALIAS_MAP: Dict[str, str] = {}


def _set_stage_alias_map(mapping: Dict[str, str]) -> None:
    """由 InferenceManager.start() 调用一次，初始化 stage→alias 映射。"""
    global _STAGE_ALIAS_MAP
    _STAGE_ALIAS_MAP.clear()
    _STAGE_ALIAS_MAP.update(mapping)


def get_stage_alias(stage_key: str) -> str:
    """返回 stage 主键对应的可读别名；未命中回退主键本身。

    若映射尚未初始化（如单元测试场景），自动从 YAML 懒加载一次。
    """
    if not _STAGE_ALIAS_MAP:
        from app.services.inference.config import load_stage_config
        from app.services.inference.stage_factory import StageFactory

        _set_stage_alias_map(StageFactory(load_stage_config()).build_stage_alias_map())
    return _STAGE_ALIAS_MAP.get(stage_key, stage_key)
