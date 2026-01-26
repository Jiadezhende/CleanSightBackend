"""
客户端队列配置加载器

统一管理客户端队列的各项参数
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any
import logging
import yaml

logger = logging.getLogger(__name__)


@dataclass
class FrameConfig:
    """帧处理配置"""
    resize_width: int = 640      # Resize宽度
    resize_height: int = 480     # Resize高度
    # 注意：inference_fps, rt_maxlen, ca_maxlen, ca_segment_len 从 inference_config.yaml 读取


@dataclass
class StateConfig:
    """状态配置"""
    initial_stage: str = "LEAK"  # 初始检测阶段
    heartbeat_timeout: int = 30  # 心跳超时（秒）


@dataclass
class ClientConfig:
    """客户端配置（统一入口）"""
    frame: FrameConfig
    state: StateConfig

    @classmethod
    def from_yaml(cls, config_path: Optional[str] = None) -> 'ClientConfig':
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
            config = cls(
                frame=FrameConfig(),
                state=StateConfig()
            )
        else:
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config_dict = yaml.safe_load(f) or {}
                logger.info("✓ 已加载client配置: %s", config_path)
                config = cls.from_dict(config_dict)
            except Exception as e:
                logger.error("✗ 加载配置文件失败: %s，使用默认配置", e)
                config = cls(
                    frame=FrameConfig(),
                    state=StateConfig()
                )

        # 从inference config读取共享参数
        config._load_shared_params_from_inference()

        # 输出配置日志和验证
        config._log_loaded_config()
        config._validate_config()

        return config

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> 'ClientConfig':
        """从字典构造配置对象"""
        frame = FrameConfig(**config_dict.get('frame', {}))
        state = StateConfig(**config_dict.get('state', {}))
        return cls(frame=frame, state=state)

    def _load_shared_params_from_inference(self):
        """从inference配置加载共享参数（队列、帧率等）

        所有跨模块共享的参数统一在 inference_config.yaml 的 global 部分定义
        """
        try:
            from app.services.inference.config import load_stage_config
            inference_config = load_stage_config()

            # 从inference config读取共享参数
            self._rt_maxlen = inference_config.rt_maxlen
            self._ca_maxlen = inference_config.ca_maxlen
            self._ca_segment_len = int(inference_config.ca_segment_seconds * inference_config.raw_fps)
            self._inference_fps = inference_config.inference_fps

            logger.debug("✓ 已从inference_config.yaml读取共享参数: rt=%d, ca=%d, segment=%d, fps=%d",
                        self._rt_maxlen, self._ca_maxlen, self._ca_segment_len, self._inference_fps)
        except Exception as e:
            # 如果无法加载inference配置，使用默认值
            logger.warning("✗ 无法从inference配置读取共享参数，使用默认值: %s", e)
            self._rt_maxlen = 30
            self._ca_maxlen = 2700
            self._ca_segment_len = 300
            self._inference_fps = 20

    @property
    def rt_maxlen(self) -> int:
        """RT队列最大长度（从inference config读取）"""
        return getattr(self, '_rt_maxlen', 30)

    @property
    def ca_maxlen(self) -> int:
        """CA队列最大长度（从inference config读取）"""
        return getattr(self, '_ca_maxlen', 2700)

    @property
    def ca_segment_len(self) -> int:
        """HLS段长度（从inference config读取）"""
        return getattr(self, '_ca_segment_len', 300)

    @property
    def inference_fps(self) -> int:
        """推理帧率（从inference config读取）"""
        return getattr(self, '_inference_fps', 20)

    def _log_loaded_config(self):
        """输出加载的配置"""
        logger.info("========== Client配置 ==========")
        logger.info("队列: rt=%d, ca=%d, segment=%d",
                   self.rt_maxlen, self.ca_maxlen, self.ca_segment_len)
        logger.info("帧处理: %dx%d, inference_fps=%d",
                   self.frame.resize_width, self.frame.resize_height, self.inference_fps)
        logger.info("状态: stage=%s, timeout=%ds",
                   self.state.initial_stage, self.state.heartbeat_timeout)
        logger.info("📌 队列/fps参数来源: inference_config.yaml (global.*)")
        logger.info("==================================")

    def _validate_config(self):
        """配置验证"""
        warnings = []

        # 检查队列容量合理性
        if self.ca_maxlen < 300:
            warnings.append(
                f"⚠️  CA队列容量过小: {self.ca_maxlen}，建议>=2700（90秒缓存）"
            )

        if self.rt_maxlen < 10:
            warnings.append(
                f"⚠️  RT队列容量过小: {self.rt_maxlen}，建议>=30（1秒缓存）"
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
            global_inference_fps = getattr(settings, 'inference_fps', None)
            if global_inference_fps and abs(self.inference_fps - int(global_inference_fps)) > 1:
                warnings.append(
                    f"⚠️  配置冲突: client.inference_fps({self.inference_fps}) "
                    f"!= settings.inference_fps({global_inference_fps})"
                )
        except Exception:
            pass

        # 输出警告
        if warnings:
            logger.warning("========== 配置问题检测 ==========")
            for warning in warnings:
                logger.warning(warning)
            logger.warning("===================================")


# 全局单例（延迟加载）
_global_config: Optional[ClientConfig] = None


def get_client_config() -> ClientConfig:
    """获取全局客户端配置（单例模式）"""
    global _global_config
    if _global_config is None:
        _global_config = ClientConfig.from_yaml()
    return _global_config
