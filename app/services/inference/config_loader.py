"""推理服务配置加载器

负责从配置文件中加载 Stage → Model → TemporalAnalyzer 的绑定关系，
实现配置驱动的架构，解耦具体实现与配置。

使用方式：
    from app.services.inference.config_loader import load_stage_config

    config = load_stage_config("stages_config.yaml")
    # 或使用默认配置
    config = load_stage_config()
"""

import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml


class StageConfig:
    """单个 Stage 的配置"""

    def __init__(self, stage_name: str, config_dict: Dict[str, Any]):
        self.stage_name = stage_name
        self.models: List[Dict[str, Any]] = config_dict.get("models", [])
        self.temporal_analyzer: Optional[Dict[str, Any]] = config_dict.get(
            "temporal_analyzer"
        )
        self.visualizer: Optional[Dict[str, Any]] = config_dict.get("visualizer")
        self.alarm_triggers: List[Dict[str, Any]] = config_dict.get(
            "alarm_triggers", []
        )

    def __repr__(self):
        return f"StageConfig(stage={self.stage_name}, models={len(self.models)})"


class InferenceConfig:
    """完整的推理服务配置"""

    def __init__(self, config_dict: Dict[str, Any]):
        self.stages: Dict[str, StageConfig] = {}
        for stage_name, stage_config in config_dict.get("stages", {}).items():
            self.stages[stage_name] = StageConfig(stage_name, stage_config)

    def get_stage_config(self, stage_name: str) -> Optional[StageConfig]:
        """获取指定 Stage 的配置"""
        return self.stages.get(stage_name)

    def list_stages(self) -> List[str]:
        """列出所有 Stage 名称"""
        return list(self.stages.keys())

    def __repr__(self):
        return f"InferenceConfig(stages={list(self.stages.keys())})"


def _expand_env_vars(config: Any) -> Any:
    """递归展开配置中的环境变量（${VAR_NAME} 格式）"""
    if isinstance(config, dict):
        return {k: _expand_env_vars(v) for k, v in config.items()}
    elif isinstance(config, list):
        return [_expand_env_vars(item) for item in config]
    elif isinstance(config, str):
        # 支持 ${VAR_NAME} 和 ${VAR_NAME:default_value} 格式
        if config.startswith("${") and config.endswith("}"):
            var_expr = config[2:-1]
            if ":" in var_expr:
                var_name, default = var_expr.split(":", 1)
                return os.environ.get(var_name, default)
            else:
                return os.environ.get(var_expr, config)
        return config
    else:
        return config


def load_stage_config(config_path: Optional[str] = None) -> InferenceConfig:
    """加载 Stage 配置文件

    Args:
        config_path: 配置文件路径（支持 YAML/JSON）
                     如果为 None，则使用默认配置

    Returns:
        InferenceConfig 对象

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置文件格式错误
    """
    if config_path is None:
        # 默认配置路径
        config_path = str(
            Path(__file__).parent.parent.parent / "config" / "stages_config.yaml"
        )

    config_file = Path(config_path)

    # 如果配置文件不存在，返回默认配置
    if not config_file.exists():
        print(f"[ConfigLoader] 配置文件不存在: {config_path}，使用默认配置")
        return _create_default_config()

    # 加载配置文件
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            if config_file.suffix in [".yaml", ".yml"]:
                config_dict = yaml.safe_load(f)
            elif config_file.suffix == ".json":
                import json

                config_dict = json.load(f)
            else:
                raise ValueError(f"不支持的配置文件格式: {config_file.suffix}")

        # 展开环境变量
        config_dict = _expand_env_vars(config_dict)

        print(f"[ConfigLoader] 成功加载配置文件: {config_path}")
        return InferenceConfig(config_dict)

    except Exception as e:
        print(f"[ConfigLoader] 加载配置文件失败: {e}，使用默认配置")
        return _create_default_config()


def _create_default_config() -> InferenceConfig:
    """创建默认配置（用于向后兼容）"""
    from app.settings import settings

    default_config = {
        "stages": {
            "LEAK": {
                "models": [
                    {
                        "name": "bubble_detection",
                        "class": "app.services.ai_models.bubble_task.BubbleDetectionTask",
                        "params": {
                            "model_path": settings.bubble_model_path,
                            "conf_threshold": settings.bubble_conf_threshold,
                            "iou_threshold": settings.bubble_iou_threshold,
                            "enabled": True,
                        },
                    },
                    {
                        "name": "bending_detection",
                        "class": "app.services.ai_models.yolo_task.EndoscopeBendingDetectionTask",
                        "params": {
                            "model_path": settings.yolo_model_path,
                            "conf_threshold": settings.yolo_conf_threshold,
                            "iou_threshold": settings.yolo_iou_threshold,
                            "enabled": True,
                        },
                    },
                ],
                "temporal_analyzer": {
                    "class": "app.services.inference.temporal_analyzer.DefaultTemporalAnalyzer",
                    "config": {
                        "bubble": {"mode": "consecutive", "threshold": 3},
                        "bending": {
                            "mode": "sliding_window",
                            "window_seconds": 2.0,
                            "ratio": 0.7,
                        },
                    },
                },
                "visualizer": {
                    "class": "app.services.ai.DefaultVisualizer",
                },
                "alarm_triggers": [
                    {
                        "condition": "bubble_detected == True",
                        "alarm_type": "流程违规",
                        "alarm_message": "检测到气泡",
                    },
                    {
                        "condition": "bending_detected == True",
                        "alarm_type": "流程违规",
                        "alarm_message": "检测到内镜弯折",
                    },
                ],
            },
            "CLEAN": {
                "models": [],
                "temporal_analyzer": {
                    "class": "app.services.inference.temporal_analyzer.DefaultTemporalAnalyzer",
                    "config": {
                        "quality": {
                            "mode": "sliding_window",
                            "window_seconds": 2.0,
                            "ratio": 0.8,
                        }
                    },
                },
            },
        }
    }

    return InferenceConfig(default_config)


def instantiate_from_config(config: Dict[str, Any]) -> Any:
    """根据配置字典实例化对象

    配置格式：
        {
            "class": "module.path.ClassName",
            "params": {...}  # 可选
        }

    Args:
        config: 配置字典

    Returns:
        实例化的对象

    Raises:
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
    import importlib

    module = importlib.import_module(module_path)
    cls = getattr(module, class_name)

    # 实例化对象
    params = config.get("params", {})
    if "config" in config and "params" not in config:
        # 兼容 temporal_analyzer 的配置格式
        params = {"config": config["config"]}

    return cls(**params)


# 示例用法
if __name__ == "__main__":
    # 加载配置
    config = load_stage_config()
    print(config)

    # 遍历所有 Stage
    for stage_name in config.list_stages():
        stage_config = config.get_stage_config(stage_name)
        print(f"\nStage: {stage_name}")
        print(f"  Models: {len(stage_config.models)}")
        for model_cfg in stage_config.models:
            print(f"    - {model_cfg['name']}: {model_cfg['class']}")

        if stage_config.temporal_analyzer:
            print(
                f"  Temporal Analyzer: {stage_config.temporal_analyzer.get('class')}"
            )
