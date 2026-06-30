"""
客户端队列配置加载器

统一管理客户端队列的各项参数
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class FrameConfig:
    """帧处理配置"""

    resize_width: int = 640  # Resize宽度
    resize_height: int = 480  # Resize高度
    # 注意：inference_fps, ca_maxlen, ca_segment_len 从 app/settings.py 读取（单一真源）


@dataclass
class StateConfig:
    """状态配置"""

    # 注意：初始 stage 不在此配置——未分配任务的客户端默认 MOCK 透传，
    # 由 ClientQueues(initial_stage="MOCK") 硬编码兜底，无可配语义。
    heartbeat_timeout: int = 30  # 心跳超时（秒）


@dataclass
class ClientConfig:
    """客户端配置（统一入口）"""

    frame: FrameConfig
    state: StateConfig

    @classmethod
    def from_yaml(cls, config_path: Optional[Path] = None) -> "ClientConfig":
        """从YAML配置文件加载

        Args:
            config_path: YAML配置文件路径，默认为 config/client_config.yaml

        Returns:
            ClientConfig实例
        """
        if config_path is None:
            base_dir = Path(__file__).parent.parent.parent.parent.resolve()
            config_path = base_dir / "config" / "client_config.yaml"

        # 加载YAML
        config_file = Path(config_path)
        if not config_file.exists():
            logger.warning("✗ 配置文件不存在: %s，使用默认配置", config_path)
            config = cls(frame=FrameConfig(), state=StateConfig())
        else:
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    loaded_data = yaml.safe_load(f)

                # 类型检查：确保加载的是字典
                if not isinstance(loaded_data, dict):
                    logger.warning(
                        "✗ 配置格式错误(非字典): %s，使用默认配置", config_path
                    )
                    config = cls(frame=FrameConfig(), state=StateConfig())
                else:
                    logger.info("✓ 已加载client配置: %s", config_path)
                    config = cls.from_dict(loaded_data)
            except Exception as e:
                logger.error("✗ 加载配置文件失败: %s，使用默认配置", e, exc_info=True)
                config = cls(frame=FrameConfig(), state=StateConfig())

        # 输出配置日志和验证
        config._log_loaded_config()
        config._validate_config()

        return config

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "ClientConfig":
        """从字典构造配置对象"""
        frame = FrameConfig(**config_dict.get("frame", {}))
        state = StateConfig(**config_dict.get("state", {}))
        return cls(frame=frame, state=state)

    @property
    def ca_maxlen(self) -> int:
        """CA队列最大长度（从 settings 单一真源读取）"""
        from app.settings import settings
        return settings.ca_maxlen

    @property
    def ca_segment_len(self) -> int:
        """HLS段长度（帧数，从 settings 单一真源读取）"""
        from app.settings import settings
        return settings.ca_segment_len

    @property
    def inference_fps(self) -> int:
        """推理帧率（从 settings 单一真源读取）"""
        from app.settings import settings
        return settings.inference_fps

    @property
    def raw_fps(self) -> int:
        """原始/解码帧率（从 settings 单一真源读取；抽帧降采样率 = inference_fps/raw_fps）"""
        from app.settings import settings
        return settings.raw_fps

    def _log_loaded_config(self):
        """输出加载的配置"""
        # DEBUG级别显示详细配置
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("========== Client配置 ==========")
            logger.debug(
                "队列: ca=%d, segment=%d",
                self.ca_maxlen,
                self.ca_segment_len,
            )
            logger.debug(
                "帧处理: %dx%d, inference_fps=%d",
                self.frame.resize_width,
                self.frame.resize_height,
                self.inference_fps,
            )
            logger.debug(
                "状态: timeout=%ds",
                self.state.heartbeat_timeout,
            )
            logger.debug("📌 队列/fps参数来源: app/settings.py (单一真源)")
            logger.debug("==================================")

    def _validate_config(self):
        """配置验证"""
        warnings = []

        # 检查队列容量合理性
        if self.ca_maxlen < 300:
            warnings.append(
                f"⚠️  CA队列容量过小: {self.ca_maxlen}，建议>=2700（90秒缓存）"
            )

        # 检查ca_segment_len是否合理
        if self.ca_segment_len > self.ca_maxlen:
            warnings.append(
                f"❌ ca_segment_len({self.ca_segment_len}) > ca_maxlen({self.ca_maxlen})，"
                f"会导致永远无法触发分段"
            )

        # 检查inference_fps与全局配置冲突
        try:
            from app.settings import settings

            global_inference_fps = getattr(settings, "inference_fps", None)
            if (
                global_inference_fps
                and abs(self.inference_fps - int(global_inference_fps)) > 1
            ):
                warnings.append(
                    f"⚠️  配置冲突: client.inference_fps({self.inference_fps}) "
                    f"!= settings.inference_fps({global_inference_fps})"
                )
        except Exception:
            pass

        # 输出警告
        if warnings:
            logger.warning("[ClientConfig] Configuration warnings detected:")
            for warning in warnings:
                logger.warning("[ClientConfig] %s", warning)
            logger.warning("===================================")


# 全局单例（延迟加载）
_global_config: Optional[ClientConfig] = None


def get_client_config() -> ClientConfig:
    """获取全局客户端配置（单例模式）"""
    global _global_config
    if _global_config is None:
        _global_config = ClientConfig.from_yaml()
    return _global_config
