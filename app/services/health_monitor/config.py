"""
健康监控配置加载器

职责：
- 加载 config/health_monitor_config.yaml
- 提供默认值（与 HealthMonitorConfig 类默认值一致）
- 单例模式，全局共享配置
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class HealthMonitorConfig:
    """健康监控配置"""

    check_interval: float = 1.0
    heartbeat_timeout: float = 5.0
    reconnect_interval: float = 5.0
    max_reconnect_attempts: int = 5
    orphan_timeout: float = 30.0

    @classmethod
    def from_yaml(cls, config_path: Optional[Path] = None) -> "HealthMonitorConfig":
        """从 YAML 加载配置"""
        if config_path is None:
            # 使用 Path 对象确保路径正确解析
            base_dir = Path(__file__).parent.parent.parent.parent.resolve()
            config_path = base_dir / "config" / "health_monitor_config.yaml"

        # 转换为 Path 对象
        config_file = Path(config_path)

        if not config_file.exists():
            logger.warning(
                f"[HealthMonitorConfig] Config file not found: {config_file}, using defaults"
            )
            return cls()

        try:
            with open(config_file, "r", encoding="utf-8") as f:
                config_data = yaml.safe_load(f)

            # 类型检查：确保加载的是字典
            if not isinstance(config_data, dict):
                logger.warning(
                    f"[HealthMonitorConfig] Invalid config format (not a dict), using defaults"
                )
                return cls()

            monitor_config = config_data.get("monitor", {})

            config = cls(
                check_interval=monitor_config.get("check_interval", 1.0),
                heartbeat_timeout=monitor_config.get("heartbeat_timeout", 5.0),
                reconnect_interval=monitor_config.get("reconnect_interval", 5.0),
                max_reconnect_attempts=monitor_config.get("max_reconnect_attempts", 5),
                orphan_timeout=monitor_config.get("orphan_timeout", 30.0),
            )

            logger.info(
                f"[HealthMonitorConfig] Loaded from {config_file}:\n"
                f"  check_interval: {config.check_interval}s\n"
                f"  heartbeat_timeout: {config.heartbeat_timeout}s\n"
                f"  reconnect_interval: {config.reconnect_interval}s\n"
                f"  max_reconnect_attempts: {config.max_reconnect_attempts}\n"
                f"  orphan_timeout: {config.orphan_timeout}s"
            )

            return config

        except Exception as e:
            logger.error(
                f"[HealthMonitorConfig] Failed to load config: {e}, using defaults"
            )
            return cls()


# 全局配置实例（单例模式）
_global_config: Optional[HealthMonitorConfig] = None


def get_health_monitor_config() -> HealthMonitorConfig:
    """获取全局健康监控配置（单例模式）"""
    global _global_config
    if _global_config is None:
        _global_config = HealthMonitorConfig.from_yaml()
    return _global_config
