"""推理服务配置加载器

负责从配置文件中加载 Stage → detectors(流源) / rules(流算子) 的绑定关系，
实现配置驱动的架构，解耦具体实现与配置。

使用方式：
    from app.services.inference.config import load_stage_config

    config = load_stage_config("stages_config.yaml")
    # 或使用默认配置
    config = load_stage_config()
"""

import logging
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml

logger = logging.getLogger(__name__)

# 全局配置缓存（单例模式）
_global_inference_config: Optional["InferenceConfig"] = None


class StageConfig:
    """单个 Stage 的配置（流处理框架：detectors 流源 / rules 流算子 / offline 占位）"""

    def __init__(self, stage_name: str, config_dict: Dict[str, Any]):
        self.stage_name = stage_name
        # 可读别名（写告警 step_name + 可视化叠字）；缺省回退主键本身
        self.alias: str = config_dict.get("alias") or stage_name
        # 流源：detector 条目（name/class/params），产出按 name 索引的流
        self.detectors: List[Dict[str, Any]] = config_dict.get("detectors") or []
        # 流算子：rule 条目（name/subscribes/class/params/realtime），一条规则一个 Operator
        self.rules: List[Dict[str, Any]] = config_dict.get("rules") or []
        # 离线段（segmenter）占位，本次不实现
        self.offline: Dict[str, Any] = config_dict.get("offline") or {}

    def __repr__(self):
        return (
            f"StageConfig(stage={self.stage_name}, "
            f"detectors={len(self.detectors)}, rules={len(self.rules)})"
        )


class InferenceConfig:
    """完整的推理服务配置"""

    def __init__(self, config_dict: Dict[str, Any]):
        self.stages: Dict[str, StageConfig] = {}
        for stage_name, stage_config in config_dict.get("stages", {}).items():
            self.stages[stage_name] = StageConfig(stage_name, stage_config)

        # 全局配置
        self.global_config: Dict[str, Any] = config_dict.get("global", {})
        self.batch_size: int = self.global_config.get("batch_size", 4)
        self.inference_decimation: int = self.global_config.get(
            "inference_decimation", 2
        )
        self.visualization_decimation: int = self.global_config.get(
            "visualization_decimation", 1
        )
        self.alarm_config: Dict[str, Any] = self.global_config.get("alarm", {})

        # 从global配置提取参数（新增）
        self.raw_fps: int = self.global_config.get("raw_fps", 30)
        self.inference_fps: int = self.global_config.get("inference_fps", 20)
        self.ca_maxlen: int = self.global_config.get("ca_maxlen", 600)
        self.ca_segment_len: int = self.global_config.get("ca_segment_len", 300)  # 帧数

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
        pattern = r"\$\{([^}]+)\}"

        def replace_var(match: re.Match[str]) -> str:
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


def load_stage_config(
    config_path: Optional[Path] = None, force_reload: bool = False
) -> InferenceConfig:
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

    # inference_config.yaml 是必备配置(单一真源)，缺失即 fail-fast，
    # 不再用代码内默认兜底——那只会用一份必然漂移的副本掩盖部署缺陷。
    if not config_file.exists():
        raise FileNotFoundError(
            f"推理配置文件不存在: {config_path}。"
            "inference_config.yaml 为必备单一真源，请检查部署。"
        )

    # 加载配置文件（任何解析/格式错误均向上抛，由上层 fail-fast）
    try:
        with open(config_file, "r", encoding="utf-8") as f:
            if config_file.suffix in [".yaml", ".yml"]:
                loaded_data = yaml.safe_load(f)
            elif config_file.suffix == ".json":
                import json

                loaded_data = json.load(f)
            else:
                raise ValueError(f"不支持的配置文件格式: {config_file.suffix}")

        # 类型检查：确保加载的是字典
        if not isinstance(loaded_data, dict):
            raise ValueError(f"推理配置格式错误(顶层非字典): {config_path}")

        # 展开环境变量
        config_dict = _expand_env_vars(loaded_data)

        logger.info("✓ 已加载inference配置: %s", config_path)
        inference_config = InferenceConfig(config_dict)

        # 输出配置日志
        _log_loaded_config(inference_config)

        # 缓存配置（单例）
        _global_inference_config = inference_config
        return inference_config

    except Exception as e:
        logger.error("✗ 加载推理配置失败: %s", e, exc_info=True)
        raise


def _log_loaded_config(config: "InferenceConfig"):
    """输出加载的配置（启动时显示）"""
    # INFO级别显示关键参数汇总
    logger.info(
        "[InferenceConfig] Loaded | stages=%d (defined), fps=%.1f/%d, batch=%d",
        len(config.list_stages()),
        config.raw_fps,
        config.inference_fps,
        config.batch_size,
    )
    
    # DEBUG级别显示详细配置
    if logger.isEnabledFor(logging.DEBUG):
        logger.debug("========== Inference配置 ==========")
        logger.debug("Stage数量: %d", len(config.list_stages()))
        logger.debug(
            "FPS配置: raw_fps=%.1f, inference_fps=%d", config.raw_fps, config.inference_fps
        )
        logger.debug(
            "队列配置: ca_maxlen=%d", config.ca_maxlen
        )
        logger.debug(
            "批处理: batch_size=%d, decimation=%d",
            config.batch_size,
            config.inference_decimation,
        )
        logger.debug("📌 此文件为所有模块共享参数的单一数据源")
        logger.debug("=====================================")


# 示例用法
if __name__ == "__main__":
    # 加载配置
    config = load_stage_config()
    print(config)

    # 遍历所有 Stage
    for stage_name in config.list_stages():
        stage_config = config.get_stage_config(stage_name)

        # 防御性编程：检查配置是否存在
        if stage_config is None:
            print(f"\nStage: {stage_name} - 配置缺失，跳过")
            continue

        print(f"\nStage: {stage_name} (alias={stage_config.alias})")
        print(f"  Detectors: {len(stage_config.detectors)}, Rules: {len(stage_config.rules)}")
        for det_cfg in stage_config.detectors:
            print(f"    - detector {det_cfg['name']}: {det_cfg['class']}")
        for rule_cfg in stage_config.rules:
            print(f"    - rule {rule_cfg['name']} subscribes={rule_cfg.get('subscribes')}: {rule_cfg['class']}")
