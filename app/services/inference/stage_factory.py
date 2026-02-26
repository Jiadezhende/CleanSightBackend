"""Stage 工厂 - 根据配置为每个 Stage 创建并组装模型实例"""

import importlib
import logging
from typing import Any, Dict, List

from app.services.inference.config import InferenceConfig

logger = logging.getLogger(__name__)


class StageFactory:
    """根据 InferenceConfig 为指定 Stage 实例化所有模型"""

    def __init__(self, config: InferenceConfig):
        self.config = config

    def create_models_for_stage(self, stage_name: str) -> List[Any]:
        """为指定 Stage 创建所有模型实例"""
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config:
            logger.warning("Stage '%s' 配置不存在", stage_name)
            return []

        models = []
        for model_cfg in stage_config.models:
            try:
                model = _instantiate_from_config(model_cfg)
                models.append(model)
                logger.info("✓ 成功创建模型: %s", model_cfg["name"])
            except Exception as e:
                logger.error("✗ 创建模型失败 %s: %s", model_cfg["name"], e)

        return models


def _instantiate_from_config(config: Dict[str, Any]) -> Any:
    """根据配置字典实例化对象

    配置格式：
        {
            "class": "module.path.ClassName",
            "params": {...}  # 可选，构造函数参数
            "config": {...}  # 可选，如果存在则作为 config 参数传递
        }
    """
    class_path = config.get("class")
    if not class_path:
        raise ValueError("配置中缺少 'class' 字段")

    parts = class_path.rsplit(".", 1)
    if len(parts) != 2:
        raise ValueError(f"无效的类路径: {class_path}")

    module_path, class_name = parts

    try:
        module = importlib.import_module(module_path)
        cls = getattr(module, class_name)
    except ImportError as e:
        raise ImportError(f"无法导入模块 {module_path}: {e}")
    except AttributeError as e:
        raise AttributeError(f"模块 {module_path} 中不存在类 {class_name}: {e}")

    if "config" in config and "params" not in config:
        kwargs = {"config": config["config"]}
    else:
        kwargs = config.get("params", {})

    try:
        return cls(**kwargs)
    except Exception as e:
        raise RuntimeError(f"实例化 {class_path} 失败: {e}")
