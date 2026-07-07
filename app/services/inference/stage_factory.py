"""Stage 工厂 - 流处理框架：为每个 Stage 创建流源 Detector 和流算子 Operator。"""

import importlib
import logging
from typing import Any, Dict, List, Tuple, Type, TYPE_CHECKING

from app.services.inference.config import InferenceConfig
from app.domain.alarm import AlarmMetric

if TYPE_CHECKING:
    from app.services.inference.offline.base import OfflineSegmenter

logger = logging.getLogger(__name__)


class StageFactory:
    """根据 InferenceConfig 为指定 Stage 实例化所有 Detector 和 Operator。"""

    def __init__(self, config: InferenceConfig):
        self.config = config

    def create_detectors_for_stage(self, stage_name: str) -> List[Any]:
        """为指定 Stage 创建所有 Detector 实例（流源，共享，推理线程 + 可视化线程）。"""
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config:
            logger.warning("Stage '%s' 配置不存在", stage_name)
            return []

        detectors = []
        for det_cfg in stage_config.detectors:
            try:
                detector = _instantiate_from_config(det_cfg)
                detectors.append(detector)
                logger.info("✓ 成功创建 Detector: %s", det_cfg.get("name", "?"))
            except Exception as e:
                logger.error("✗ 创建 Detector 失败 %s: %s", det_cfg.get("name", "?"), e, exc_info=True)

        return detectors

    def create_operators_for_stage(
        self, stage_name: str
    ) -> List[Tuple[Type, Dict[str, Any]]]:
        """为指定 Stage 返回流算子 Operator 的实例化规格（每条规则一个）。

        调用方（set_task）用这些 spec 按 Client 创建独立实例：
            for cls, kwargs in specs:
                operator = cls(**kwargs)   # kwargs 含 name/subscribes/params

        Returns:
            List of (OperatorClass, kwargs) tuples。
            rule.subscribes 显式必填（输入流名 = detector.name）；缺失则 fail-fast 跳过该规则。
            rule.name 注入为算子自身/输出身份，subscribes 注入为输入流清单。
        """
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config:
            logger.warning("Stage '%s' 配置不存在", stage_name)
            return []

        specs: List[Tuple[Type, Dict[str, Any]]] = []
        for rule_cfg in stage_config.rules:
            name = rule_cfg.get("name", "")
            class_path = rule_cfg.get("class")
            if not class_path:
                logger.error("✗ rule '%s' 缺少 class 字段，跳过", name)
                continue
            subscribes = rule_cfg.get("subscribes")
            if not subscribes:
                logger.error("✗ rule '%s' 必须显式声明 subscribes（输入流），跳过", name)
                continue
            try:
                cls = _import_class(class_path)
                kwargs = dict(rule_cfg.get("params") or {})
                kwargs.setdefault("name", name)
                kwargs["subscribes"] = list(subscribes)
                specs.append((cls, kwargs))
                logger.info("✓ 注册 Operator spec: %s (subscribes=%s)", name, subscribes)
            except Exception as e:
                logger.error("✗ 注册 Operator 失败 %s: %s", name, e, exc_info=True)

        return specs

    def create_offline_segmenter(
        self, stage_name: str, override_class: str | None = None
    ) -> "OfflineSegmenter | None":
        """为指定 Stage 实例化离线分割策略（`stages.<step_id>.offline`），未启用返回 None。

        offline 配置 schema（`{}` 或 `enabled:false` = 不启用该 stage 离线分段）：
            offline:
              enabled: true
              name: <segmenter 身份，= SegmentFact.source>
              subscribes: [<detector.name>, ...]   # 必须全命中同 stage detector
              class: <OfflineSegmenter 子类全限定路径>
              params: {...}                         # 原样传入实现类

        工厂注入 `name`/`subscribes`，`params` 不得重复声明这两键。`override_class` 支撑
        CLI `--strategy`（开发期对比不同策略），覆盖配置 `class` 但沿用同一 name/subscribes。
        配置缺字段 / 未知 detector / 类加载失败一律 **fail-fast**（抛异常，不静默跳过）。

        Args:
            stage_name: stage 主键（= str(step_id)）
            override_class: 可选，覆盖配置里的 class 全限定路径
        """
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config:
            raise ValueError(f"Stage '{stage_name}' 配置不存在")

        offline = stage_config.offline or {}
        if not offline or not offline.get("enabled", False):
            return None

        name = offline.get("name")
        subscribes = offline.get("subscribes")
        class_path = override_class or offline.get("class")
        if not name:
            raise ValueError(f"Stage '{stage_name}' offline 缺少 name")
        if not subscribes:
            raise ValueError(f"Stage '{stage_name}' offline 缺少 subscribes")
        if not class_path:
            raise ValueError(f"Stage '{stage_name}' offline 缺少 class")

        # subscribes 必须全部命中同 stage 的 detector name
        detector_names = {d.get("name") for d in stage_config.detectors}
        unknown = [s for s in subscribes if s not in detector_names]
        if unknown:
            raise ValueError(
                f"Stage '{stage_name}' offline subscribes 引用未知 detector: {unknown}"
            )

        params = dict(offline.get("params") or {})
        for reserved in ("name", "subscribes"):
            if reserved in params:
                raise ValueError(
                    f"Stage '{stage_name}' offline params 不得重复声明 '{reserved}'（工厂注入）"
                )

        cls = _import_class(class_path)
        segmenter = cls(name=name, subscribes=list(subscribes), **params)
        logger.info(
            "✓ 创建 OfflineSegmenter: %s (class=%s, subscribes=%s)",
            name, class_path, subscribes,
        )
        return segmenter

    def build_task_metric_map(self) -> Dict[str, AlarmMetric]:
        """构建 stream_name(detector.name) → AlarmMetric 映射（signals_10s 唯一定义处）。

        key 必须是 detector.name（= slide_window key），由 realtime:true 规则的 subscribes 取得。
        realtime: false 的规则（如 bending_check）只产结算告警，不纳入 signals_10s。
        无法映射到 AlarmMetric 的流名直接跳过。
        """
        mapping: Dict[str, AlarmMetric] = {}
        for stage_cfg in self.config.stages.values():
            for rule_cfg in stage_cfg.rules:
                if not rule_cfg.get("realtime", True):
                    continue
                for stream_name in rule_cfg.get("subscribes") or []:
                    try:
                        mapping[stream_name] = AlarmMetric(stream_name.upper())
                    except ValueError:
                        logger.warning(
                            "[StageFactory] stream '%s' has no AlarmMetric mapping, "
                            "excluded from signals_10s",
                            stream_name,
                        )
        return mapping

    def build_stage_alias_map(self) -> Dict[str, str]:
        """构建 stage 主键(step_id) → alias 映射（唯一定义处）。

        alias 仅用于可读性出口（写告警 step_name + 可视化叠字）；功能性标识一律用主键。
        缺省 alias 的 stage 回退主键本身（见 StageConfig.alias）。
        """
        return {name: cfg.alias for name, cfg in self.config.stages.items()}


def _import_class(class_path: str) -> Type:
    """根据完整类路径导入并返回类对象。"""
    parts = class_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"无效的类路径: {class_path}")
    module_path, class_name = parts
    try:
        module = importlib.import_module(module_path)
        return getattr(module, class_name)
    except ImportError as e:
        raise ImportError(f"无法导入模块 {module_path}: {e}")
    except AttributeError as e:
        raise AttributeError(f"模块 {module_path} 中不存在类 {class_name}: {e}")


def _instantiate_from_config(config: Dict[str, Any]) -> Any:
    """根据配置字典实例化 Detector 对象。

    配置格式：
        {
            "class": "module.path.ClassName",
            "params": {...}  # 可选
        }
    """
    class_path = config.get("class")
    if not class_path:
        raise ValueError("配置中缺少 'class' 字段")

    cls = _import_class(class_path)

    if "config" in config and "params" not in config:
        kwargs = {"config": config["config"]}
    else:
        kwargs = config.get("params") or {}

    try:
        return cls(**kwargs)
    except Exception as e:
        raise RuntimeError(f"实例化 {class_path} 失败: {e}")
