"""组件工厂 - 根据配置动态创建推理组件

负责根据配置文件实例化：
- 模型（BaseModel）
- 时序分析器（BaseTemporalAnalyzer）
- 可视化器（BaseVisualizer）

使用方式：
    from app.services.inference.components.component_factory import ComponentFactory

    factory = ComponentFactory(config)
    models = factory.create_models_for_stage("LEAK")
    analyzer = factory.create_temporal_analyzer_for_stage("LEAK")
    visualizer = factory.create_visualizer_for_stage("LEAK")
"""

import importlib
import logging
from typing import Any, Dict, List, Optional

from app.services.inference.config import InferenceConfig, StageConfig

logger = logging.getLogger(__name__)


class ComponentFactory:
    """组件工厂 - 根据配置动态创建推理组件"""

    def __init__(self, config: InferenceConfig):
        """初始化组件工厂

        Args:
            config: 推理服务配置对象
        """
        self.config = config

    def create_models_for_stage(self, stage_name: str) -> List[Any]:
        """为指定 Stage 创建所有模型实例

        Args:
            stage_name: Stage 名称（如 "LEAK", "CLEAN"）

        Returns:
            模型实例列表
        """
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config:
            logger.warning("Stage '%s' 配置不存在", stage_name)
            return []

        models = []
        for model_cfg in stage_config.models:
            try:
                model = self._instantiate_from_config(model_cfg)
                models.append(model)
                logger.info("✓ 成功创建模型: %s", model_cfg["name"])
            except Exception as e:
                logger.error("✗ 创建模型失败 %s: %s", model_cfg["name"], e)

        return models

    def create_temporal_analyzer_for_stage(self, stage_name: str) -> Optional[Any]:
        """为指定 Stage 创建时序分析器实例

        Args:
            stage_name: Stage 名称

        Returns:
            时序分析器实例，如果配置不存在则返回 None
        """
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config or not stage_config.temporal_analyzer:
            logger.debug("Stage '%s' 无时序分析器配置", stage_name)
            return None

        try:
            analyzer = self._instantiate_from_config(stage_config.temporal_analyzer)
            logger.info("✓ 成功创建时序分析器: %s", stage_name)
            return analyzer
        except Exception as e:
            logger.error("✗ 创建时序分析器失败 %s: %s", stage_name, e)
            return None

    def create_visualizer_for_stage(self, stage_name: str) -> Optional[Any]:
        """为指定 Stage 创建可视化器实例

        Args:
            stage_name: Stage 名称

        Returns:
            可视化器实例，如果配置不存在则返回 None
        """
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config or not stage_config.visualizer:
            logger.debug("Stage '%s' 无可视化器配置", stage_name)
            return None

        try:
            visualizer = self._instantiate_from_config(stage_config.visualizer)
            logger.info("✓ 成功创建可视化器: %s", stage_name)
            return visualizer
        except Exception as e:
            logger.error("✗ 创建可视化器失败 %s: %s", stage_name, e)
            return None

    def get_alarm_triggers_for_stage(self, stage_name: str) -> List[Dict[str, Any]]:
        """获取指定 Stage 的告警触发条件

        Args:
            stage_name: Stage 名称

        Returns:
            告警触发条件列表
        """
        stage_config = self.config.get_stage_config(stage_name)
        if not stage_config:
            return []

        return stage_config.alarm_triggers

    def _instantiate_from_config(self, config: Dict[str, Any]) -> Any:
        """根据配置字典实例化对象

        配置格式：
            {
                "class": "module.path.ClassName",
                "params": {...}  # 可选，构造函数参数
                "config": {...}  # 可选，如果存在则作为 config 参数传递
            }

        Args:
            config: 配置字典

        Returns:
            实例化的对象

        Raises:
            ValueError: 配置格式错误
            ImportError: 模块导入失败
            AttributeError: 类不存在
        """
        class_path = config.get("class")
        if not class_path:
            raise ValueError("配置中缺少 'class' 字段")

        # 解析模块和类名
        parts = class_path.rsplit(".", 1)
        if len(parts) != 2:
            raise ValueError(f"无效的类路径: {class_path}")

        module_path, class_name = parts

        # 动态导入模块
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
        except ImportError as e:
            raise ImportError(f"无法导入模块 {module_path}: {e}")
        except AttributeError as e:
            raise AttributeError(f"模块 {module_path} 中不存在类 {class_name}: {e}")

        # 构造参数
        if "config" in config and "params" not in config:
            # 兼容 temporal_analyzer 的配置格式（config 参数）
            kwargs = {"config": config["config"]}
        else:
            # 使用 params 作为构造参数
            kwargs = config.get("params", {})

        # 实例化对象
        try:
            instance = cls(**kwargs)
            return instance
        except Exception as e:
            raise RuntimeError(f"实例化 {class_path} 失败: {e}")


def create_components_from_config(
    config_path: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    """从配置文件创建所有阶段的组件

    这是一个便捷函数，用于一次性创建所有 Stage 的组件。

    Args:
        config_path: 配置文件路径（可选，默认使用 stages_config.yaml）

    Returns:
        字典，格式为:
        {
            "LEAK": {
                "models": [model1, model2, ...],
                "temporal_analyzer": analyzer,
                "visualizer": visualizer,
                "alarm_triggers": [trigger1, trigger2, ...]
            },
            "CLEAN": {...}
        }
    """
    from app.services.inference.config import load_stage_config

    config = load_stage_config(config_path)
    factory = ComponentFactory(config)

    components = {}
    for stage_name in config.list_stages():
        components[stage_name] = {
            "models": factory.create_models_for_stage(stage_name),
            "temporal_analyzer": factory.create_temporal_analyzer_for_stage(stage_name),
            "visualizer": factory.create_visualizer_for_stage(stage_name),
            "alarm_triggers": factory.get_alarm_triggers_for_stage(stage_name),
        }

    return components


# 示例用法
if __name__ == "__main__":
    # 创建所有组件
    components = create_components_from_config()

    # 打印 LEAK 阶段的组件
    leak_components = components.get("LEAK", {})
    print(f"LEAK 阶段:")
    print(f"  模型数量: {len(leak_components.get('models', []))}")
    print(f"  时序分析器: {leak_components.get('temporal_analyzer')}")
    print(f"  可视化器: {leak_components.get('visualizer')}")
    print(f"  告警触发条件数量: {len(leak_components.get('alarm_triggers', []))}")
