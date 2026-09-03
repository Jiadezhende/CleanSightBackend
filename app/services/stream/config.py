"""
流处理服务配置加载器

负责流解码相关配置
注意：健康监控配置已迁移到 app/services/health_monitor/config.py
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

from app.settings import settings

logger = logging.getLogger(__name__)


@dataclass
class DecoderConfig:
    """解码器配置"""

    default_width: int = 640
    default_height: int = 480
    # 解码 CFR = 生产者源，单一真源 settings.raw_fps（ffmpeg 强制 CFR、下游全部派生）。
    # 不在 stream_config.yaml 里再写死一个 fps，否则又成第二个"巧合相等"的独立源。
    default_fps: int = field(default_factory=lambda: settings.raw_fps)
    pix_fmt: str = "bgr24"
    chunk_read_size: int = 32768
    backpressure_ratio: float = 0.90  # 队列达到90%时丢帧


@dataclass
class StreamConfig:
    """流处理配置（统一入口）"""

    decoder: DecoderConfig

    @classmethod
    def from_yaml(cls, config_path: Optional[str] = None) -> "StreamConfig":
        """从YAML配置文件加载

        Args:
            config_path: YAML配置文件路径，默认为 config/stream_config.yaml

        Returns:
            StreamConfig实例
        """
        if config_path is None:
            config_path = settings.config_dir / "stream_config.yaml"

        # 加载YAML
        config_file = Path(config_path)
        if not config_file.exists():
            logger.warning("✗ 配置文件不存在: %s，使用默认配置", config_path)
            config = cls(decoder=DecoderConfig())
        else:
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    loaded_data = yaml.safe_load(f)

                # 类型检查：确保加载的是字典
                if not isinstance(loaded_data, dict):
                    logger.warning(
                        "✗ 配置格式错误(非字典): %s，使用默认配置", config_path
                    )
                    config = cls(decoder=DecoderConfig())
                else:
                    logger.info("✓ 已加载stream配置: %s", config_path)
                    config = cls.from_dict(loaded_data)
            except Exception as e:
                logger.error("✗ 加载配置文件失败: %s，使用默认配置", e, exc_info=True)
                config = cls(decoder=DecoderConfig())

        # 输出配置日志
        config._log_loaded_config()

        return config

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "StreamConfig":
        """从字典构造配置对象"""
        decoder = DecoderConfig(**config_dict.get("decoder", {}))
        return cls(decoder=decoder)

    def _log_loaded_config(self):
        """输出加载的配置"""
        # DEBUG级别显示详细配置
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("========== Stream配置 ==========")
            logger.debug(
                "解码器: %dx%d@%dfps, backpressure=%.2f",
                self.decoder.default_width,
                self.decoder.default_height,
                self.decoder.default_fps,
                self.decoder.backpressure_ratio,
            )
            logger.debug("==================================")


# 全局单例（延迟加载）
_global_config: Optional[StreamConfig] = None


def get_stream_config() -> StreamConfig:
    """获取全局流处理配置（单例模式）"""
    global _global_config
    if _global_config is None:
        _global_config = StreamConfig.from_yaml()
    return _global_config
