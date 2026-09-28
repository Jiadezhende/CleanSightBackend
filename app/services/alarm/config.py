"""
告警服务配置模型：读 `config/persistence_config.yaml` 的 `alarm` 段
（同文件 `storage` 段归 `app.daemons.cleanup.config`）

支持从YAML文件加载配置，提供默认值
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class AlarmConfig:
    """告警上报配置"""

    workers: int = 1
    queue_size: int = 200


@dataclass
class AlarmServiceConfig:
    """告警服务配置（统一入口）"""

    alarm: AlarmConfig = field(default_factory=AlarmConfig)

    @classmethod
    def from_yaml(cls, config_path: Optional[str] = None) -> "AlarmServiceConfig":
        """从YAML配置文件加载

        Args:
            config_path: YAML配置文件路径，默认为 config/persistence_config.yaml

        Returns:
            AlarmServiceConfig实例
        """
        if config_path is None:
            from app.settings import settings

            config_path = settings.config_dir / "persistence_config.yaml"

        # 加载YAML
        config_file = Path(config_path)
        if not config_file.exists():
            # 文件不存在时使用默认配置
            logger.warning("✗ 配置文件不存在: %s，使用默认配置", config_path)
            config = cls()
        else:
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    config_dict = yaml.safe_load(f) or {}
                logger.info("✓ 已加载alarm配置: %s", config_path)
                config = cls.from_dict(config_dict)
            except Exception as e:
                logger.error("✗ 加载配置文件失败: %s，使用默认配置", e, exc_info=True)
                config = cls()

        # 输出配置日志和验证
        config._log_loaded_config()
        config._validate_config()

        return config

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "AlarmServiceConfig":
        """从字典构造配置对象

        Args:
            config_dict: 配置字典

        Returns:
            AlarmServiceConfig实例
        """
        # yaml 由 git 跟踪、每次部署整仓覆盖为干净版，磁盘不会残留已废字段；
        # 故不做字段过滤——真出未知字段就让它响亮地崩，别静默吞。
        alarm = AlarmConfig(**config_dict.get("alarm", {}))
        return cls(alarm=alarm)

    # 扁平访问器（service 唯一入口；嵌套 dataclass 仅作分组存储，全仓无嵌套访问）
    @property
    def alarm_workers(self) -> int:
        return self.alarm.workers

    @property
    def alarm_queue_size(self) -> int:
        return self.alarm.queue_size

    def _log_loaded_config(self):
        """输出加载的配置（启动时显示）"""
        # DEBUG级别显示详细配置
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("========== Alarm配置 ==========")
            logger.debug(
                "告警: workers=%d, queue=%d",
                self.alarm.workers,
                self.alarm.queue_size,
            )
            logger.debug("=====================================")

    def _validate_config(self):
        """配置验证和冲突检测"""
        warnings = []

        # 1. 检查队列容量合理性
        if self.alarm.queue_size < 64:
            warnings.append(f"⚠️  告警队列容量过小: {self.alarm.queue_size}，建议>=128")

        # 2. 检查Worker数量合理性
        if self.alarm.workers < 1:
            warnings.append(f"❌ 告警Worker数量必须>=1")

        # 注意：告警重试策略写死在 alarm_worker，无需在此验证

        # 输出警告
        if warnings:
            logger.warning("========== 配置问题检测 ==========")
            for warning in warnings:
                logger.warning(warning)
            logger.warning("=====================================")


# 全局单例（延迟加载）
_global_alarm_config: Optional[AlarmServiceConfig] = None


def get_alarm_config() -> AlarmServiceConfig:
    """获取全局告警服务配置（单例模式）"""
    global _global_alarm_config
    if _global_alarm_config is None:
        _global_alarm_config = AlarmServiceConfig.from_yaml()
    return _global_alarm_config
