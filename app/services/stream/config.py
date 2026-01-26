"""
流处理服务配置加载器

统一管理流解码、健康监控、清理等参数
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Dict, Any
import logging
import yaml

logger = logging.getLogger(__name__)


@dataclass
class DecoderConfig:
    """解码器配置"""
    default_width: int = 640
    default_height: int = 480
    default_fps: int = 30
    pix_fmt: str = "bgr24"
    chunk_read_size: int = 32768
    backpressure_ratio: float = 0.90  # 队列达到90%时丢帧


@dataclass
class HealthMonitorConfig:
    """健康监控配置"""
    check_interval: float = 5.0         # 检查间隔（秒）
    heartbeat_timeout: float = 10.0     # 心跳超时（秒）
    restart_delay: float = 2.0          # 重启延迟（秒）
    max_restart_attempts: int = 3       # 最大重启次数
    restart_window: float = 60.0        # 重启窗口（秒）


@dataclass
class CleanupConfig:
    """清理服务配置"""
    check_interval: float = 30.0        # 检查间隔（秒）
    orphan_timeout: float = 300.0       # 孤儿流超时（秒）


@dataclass
class StreamConfig:
    """流处理配置（统一入口）"""
    decoder: DecoderConfig
    health_monitor: HealthMonitorConfig
    cleanup: CleanupConfig

    @classmethod
    def from_yaml(cls, config_path: Optional[str] = None) -> 'StreamConfig':
        """从YAML配置文件加载

        Args:
            config_path: YAML配置文件路径，默认为 config/stream_config.yaml

        Returns:
            StreamConfig实例
        """
        if config_path is None:
            base_dir = Path(__file__).parent.parent.parent.parent.resolve()
            config_path = base_dir / "config" / "stream_config.yaml"

        # 加载YAML
        config_file = Path(config_path)
        if not config_file.exists():
            logger.warning("✗ 配置文件不存在: %s，使用默认配置", config_path)
            config = cls(
                decoder=DecoderConfig(),
                health_monitor=HealthMonitorConfig(),
                cleanup=CleanupConfig()
            )
        else:
            try:
                with open(config_file, 'r', encoding='utf-8') as f:
                    config_dict = yaml.safe_load(f) or {}
                logger.info("✓ 已加载stream配置: %s", config_path)
                config = cls.from_dict(config_dict)
            except Exception as e:
                logger.error("✗ 加载配置文件失败: %s，使用默认配置", e)
                config = cls(
                    decoder=DecoderConfig(),
                    health_monitor=HealthMonitorConfig(),
                    cleanup=CleanupConfig()
                )

        # 输出配置日志和验证
        config._log_loaded_config()
        config._validate_config()

        return config

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> 'StreamConfig':
        """从字典构造配置对象"""
        decoder = DecoderConfig(**config_dict.get('decoder', {}))
        health_monitor = HealthMonitorConfig(**config_dict.get('health_monitor', {}))
        cleanup = CleanupConfig(**config_dict.get('cleanup', {}))
        return cls(decoder=decoder, health_monitor=health_monitor, cleanup=cleanup)

    def _log_loaded_config(self):
        """输出加载的配置"""
        logger.info("========== Stream配置 ==========")
        logger.info("解码器: %dx%d@%dfps, backpressure=%.2f",
                   self.decoder.default_width, self.decoder.default_height,
                   self.decoder.default_fps, self.decoder.backpressure_ratio)
        logger.info("健康监控: check=%.1fs, timeout=%.1fs, max_restart=%d",
                   self.health_monitor.check_interval, self.health_monitor.heartbeat_timeout,
                   self.health_monitor.max_restart_attempts)
        logger.info("清理: check=%.1fs, orphan_timeout=%.1fs",
                   self.cleanup.check_interval, self.cleanup.orphan_timeout)
        logger.info("==================================")

    def _validate_config(self):
        """配置验证"""
        warnings = []

        # 检查监控间隔合理性
        if self.health_monitor.check_interval > self.health_monitor.heartbeat_timeout:
            warnings.append(
                f"⚠️  check_interval({self.health_monitor.check_interval}s) "
                f"> heartbeat_timeout({self.health_monitor.heartbeat_timeout}s)，"
                f"可能导致误判为超时"
            )

        # 检查重启窗口合理性
        if self.health_monitor.restart_window < self.health_monitor.restart_delay * self.health_monitor.max_restart_attempts:
            warnings.append(
                f"⚠️  restart_window过小，可能导致重启限制失效"
            )

        # 检查清理间隔合理性
        if self.cleanup.check_interval > self.cleanup.orphan_timeout:
            warnings.append(
                f"⚠️  cleanup.check_interval({self.cleanup.check_interval}s) "
                f"> orphan_timeout({self.cleanup.orphan_timeout}s)，"
                f"孤儿流可能无法及时清理"
            )

        # 输出警告
        if warnings:
            logger.warning("========== 配置问题检测 ==========")
            for warning in warnings:
                logger.warning(warning)
            logger.warning("===================================")


# 全局单例（延迟加载）
_global_config: Optional[StreamConfig] = None


def get_stream_config() -> StreamConfig:
    """获取全局流处理配置（单例模式）"""
    global _global_config
    if _global_config is None:
        _global_config = StreamConfig.from_yaml()
    return _global_config
