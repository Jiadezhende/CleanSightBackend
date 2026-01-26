"""推理服务配置加载器

负责从配置文件中加载 Stage → Model → TemporalAnalyzer 的绑定关系，
实现配置驱动的架构，解耦具体实现与配置。

使用方式：
    from app.services.inference.config import load_stage_config

    config = load_stage_config("stages_config.yaml")
    # 或使用默认配置
    config = load_stage_config()
"""

import os
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# 全局配置缓存（单例模式）
_global_inference_config: Optional['InferenceConfig'] = None


class StageConfig:
    """单个 Stage 的配置"""

    def __init__(self, stage_name: str, config_dict: Dict[str, Any]):
        self.stage_name = stage_name
        # 确保即使 models 为 None 也转换为空列表
        self.models: List[Dict[str, Any]] = config_dict.get("models") or []
        self.temporal_analyzer: Optional[Dict[str, Any]] = config_dict.get(
            "temporal_analyzer"
        )
        self.visualizer: Optional[Dict[str, Any]] = config_dict.get("visualizer")
        # 确保即使 alarm_triggers 为 None 也转换为空列表
        self.alarm_triggers: List[Dict[str, Any]] = config_dict.get(
            "alarm_triggers"
        ) or []

    def __repr__(self):
        return f"StageConfig(stage={self.stage_name}, models={len(self.models)})"


class InferenceConfig:
    """完整的推理服务配置"""

    def __init__(self, config_dict: Dict[str, Any]):
        self.stages: Dict[str, StageConfig] = {}
        for stage_name, stage_config in config_dict.get("stages", {}).items():
            self.stages[stage_name] = StageConfig(stage_name, stage_config)

        # 全局配置
        self.global_config: Dict[str, Any] = config_dict.get("global", {})
        self.batch_size: int = self.global_config.get("batch_size", 4)
        self.inference_decimation: int = self.global_config.get("inference_decimation", 2)
        self.visualization_decimation: int = self.global_config.get("visualization_decimation", 1)
        self.alarm_config: Dict[str, Any] = self.global_config.get("alarm", {})

        # 从global配置提取参数（新增）
        self.raw_fps: int = self.global_config.get("raw_fps", 30)
        self.inference_fps: int = self.global_config.get("inference_fps", 20)
        self.rt_maxlen: int = self.global_config.get("rt_maxlen", 30)
        self.ca_maxlen: int = self.global_config.get("ca_maxlen", 600)
        self.ca_segment_len: int = self.global_config.get("ca_segment_len", 300)  # 帧数
        # 兼容旧的 ca_segment_seconds 配置（废弃，优先使用 ca_segment_len）
        if "ca_segment_seconds" in self.global_config and "ca_segment_len" not in self.global_config:
            self.ca_segment_len = int(self.global_config["ca_segment_seconds"] * self.raw_fps)

    def get_stage_config(self, stage_name: str) -> Optional[StageConfig]:
        """获取指定 Stage 的配置"""
        return self.stages.get(stage_name)

    def list_stages(self) -> List[str]:
        """列出所有 Stage 名称"""
        return list(self.stages.keys())

    def get_inference_fps(self, base_fps: int = 30) -> int:
        """根据降频配置计算实际推理FPS

        Args:
            base_fps: 原始视频帧率（默认30fps）

        Returns:
            实际推理帧率
        """
        return base_fps // self.inference_decimation

    def __repr__(self):
        return f"InferenceConfig(stages={list(self.stages.keys())}, batch_size={self.batch_size}, inference_decimation={self.inference_decimation})"


def _expand_env_vars(config: Any) -> Any:
    """递归展开配置中的环境变量（${VAR_NAME} 格式）

    支持以下格式：
    - ${VAR_NAME}：直接替换为环境变量值
    - ${VAR_NAME:default}：如果环境变量不存在，使用默认值
    - ${VAR_NAME}/suffix：替换后可以拼接路径（支持多个变量）

    示例：
    - ${MODEL_PATH}/model.pt → /path/to/models/model.pt
    - ${MODEL_PATH:./weights}/model.pt → ./weights/model.pt (如果MODEL_PATH不存在)
    """
    if isinstance(config, dict):
        return {k: _expand_env_vars(v) for k, v in config.items()}
    elif isinstance(config, list):
        return [_expand_env_vars(item) for item in config]
    elif isinstance(config, str):
        # 使用正则表达式匹配所有 ${VAR_NAME} 或 ${VAR_NAME:default} 模式
        import re
        pattern = r'\$\{([^}]+)\}'

        def replace_var(match):
            var_expr = match.group(1)
            if ":" in var_expr:
                var_name, default = var_expr.split(":", 1)
                return os.environ.get(var_name, default)
            else:
                return os.environ.get(var_expr, match.group(0))

        # 替换所有匹配的环境变量
        result = re.sub(pattern, replace_var, config)
        return result
    else:
        return config


def load_stage_config(config_path: Optional[str] = None, force_reload: bool = False) -> InferenceConfig:
    """加载 Stage 配置文件（单例模式）

    Args:
        config_path: 配置文件路径（支持 YAML/JSON）
                     如果为 None，则使用默认配置
        force_reload: 是否强制重新加载（默认使用缓存）

    Returns:
        InferenceConfig 对象

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置文件格式错误
    """
    global _global_inference_config

    # 使用缓存（单例模式）
    if not force_reload and _global_inference_config is not None:
        logger.debug("使用缓存的inference配置")
        return _global_inference_config

    if config_path is None:
        # 默认配置路径（外部config目录）
        base_dir = Path(__file__).parent.parent.parent.parent.resolve()
        config_path = base_dir / "config" / "inference_config.yaml"

    config_file = Path(config_path)

    # 如果配置文件不存在，返回默认配置
    if not config_file.exists():
        logger.warning("配置文件不存在: %s，使用默认配置", config_path)
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

        logger.info("✓ 已加载inference配置: %s", config_path)
        inference_config = InferenceConfig(config_dict)

        # 输出配置日志
        _log_loaded_config(inference_config)

        # 缓存配置（单例）
        _global_inference_config = inference_config
        return inference_config

    except Exception as e:
        logger.error("✗ 加载配置文件失败: %s，使用默认配置", e)
        default_config = _create_default_config()
        _global_inference_config = default_config
        return default_config


def _log_loaded_config(config: 'InferenceConfig'):
    """输出加载的配置（启动时显示）"""
    logger.info("========== Inference配置 ==========")
    logger.info("Stage数量: %d", len(config.list_stages()))
    logger.info("FPS配置: raw_fps=%.1f, inference_fps=%d", config.raw_fps, config.inference_fps)
    logger.info("队列配置: rt_maxlen=%d, ca_maxlen=%d", config.rt_maxlen, config.ca_maxlen)
    logger.info("批处理: batch_size=%d, decimation=%d", config.batch_size, config.inference_decimation)
    logger.info("📌 此文件为所有模块共享参数的单一数据源")
    logger.info("=====================================")




def _create_default_config() -> InferenceConfig:
    """创建默认配置（用于向后兼容）"""
    # 从环境变量获取模型路径，如果未设置则使用默认值
    model_base_path = os.environ.get("CLEANSIGHT_MODEL_PATH", "./app/data")

    default_config = {
        "stages": {
            "LEAK": {
                "models": [
                    {
                        "name": "bubble_detection",
                        "class": "app.services.models.bubble.BubbleDetectionTask",
                        "params": {
                            "model_path": f"{model_base_path}/bubble-best.pt",
                            "conf_threshold": 0.5,
                            "iou_threshold": 0.45,
                            "enabled": True,
                        },
                    },
                    {
                        "name": "bending_detection",
                        "class": "app.services.models.bending.EndoscopeBendingDetectionTask",
                        "params": {
                            "model_path": f"{model_base_path}/bend-best.pt",
                            "conf_threshold": 0.6,
                            "iou_threshold": 0.45,
                            "enabled": True,
                        },
                    },
                ],
                "temporal_analyzer": {
                    "class": "app.services.inference.components.DefaultTemporalAnalyzer",
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
                    "class": "app.services.inference.components.DefaultVisualizer",
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
                    "class": "app.services.inference.components.DefaultTemporalAnalyzer",
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
