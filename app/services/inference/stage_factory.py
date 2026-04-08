"""Stage 工厂 - 根据配置为每个 Stage 创建 Detector 和 TemporalAnalyzer 实例"""

import importlib
import logging
from typing import Any, Dict, List, Tuple, Type

from app.services.inference.config import InferenceConfig

logger = logging.getLogger(__name__)


class StageFactory:
    """根据 InferenceConfig 为指定 Stage 实例化所有 Detector 和 TemporalAnalyzer。"""

    def __init__(self, config: InferenceConfig):
        self.config = config

    def create_detectors_for_stage(self, stage_name: str) -> List[Any]:
        """为指定 Stage 创建所有 Detector 实例（共享，推理线程 + 可视化线程）。"""
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config:
            logger.warning("Stage '%s' 配置不存在", stage_name)
            return []

        detectors = []
        for model_cfg in stage_config.models:
            try:
                detector = _instantiate_from_config(model_cfg)
                detectors.append(detector)
                logger.info("✓ 成功创建 Detector: %s", model_cfg.get("name", "?"))
            except Exception as e:
                logger.error("✗ 创建 Detector 失败 %s: %s", model_cfg.get("name", "?"), e, exc_info=True)

        return detectors

    def create_analyzer_specs_for_stage(
        self, stage_name: str
    ) -> List[Tuple[Type, Dict[str, Any]]]:
        """为指定 Stage 返回 TemporalAnalyzer 的 (class, kwargs) 实例化规格。

        调用方（set_task）用这些 spec 按 Client 创建独立实例：
            analyzers = [cls(**kwargs) for cls, kwargs in specs]

        Returns:
            List of (AnalyzerClass, constructor_kwargs) tuples.
            如果某个 model 条目没有 analyzer_class 字段，则跳过。
        """
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config:
            logger.warning("Stage '%s' 配置不存在", stage_name)
            return []

        specs: List[Tuple[Type, Dict[str, Any]]] = []
        for model_cfg in stage_config.models:
            analyzer_class_path = model_cfg.get("analyzer_class")
            if not analyzer_class_path:
                logger.debug(
                    "model '%s' 无 analyzer_class，跳过 TemporalAnalyzer 创建",
                    model_cfg.get("name", "?"),
                )
                continue

            try:
                cls = _import_class(analyzer_class_path)
                kwargs = model_cfg.get("analyzer_params") or {}
                specs.append((cls, kwargs))
                logger.info(
                    "✓ 注册 TemporalAnalyzer spec: %s", model_cfg.get("name", "?")
                )
            except Exception as e:
                logger.error(
                    "✗ 注册 TemporalAnalyzer 失败 %s: %s", model_cfg.get("name", "?"), e, exc_info=True
                )

        return specs


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
