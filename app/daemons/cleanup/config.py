"""
TTL 清理配置：读 `config/persistence_config.yaml` 的 `storage` 段

扫描根不在此定义：委托 `settings.storage_base_dir`（单一真源）。
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class CleanupConfig:
    """TTL 清理配置"""

    enable_cleanup: bool = False
    cleanup_days: int = 7
    cleanup_interval_seconds: int = 3600

    @classmethod
    def from_yaml(cls, config_path: Optional[str] = None) -> "CleanupConfig":
        """从YAML配置文件加载

        Args:
            config_path: YAML配置文件路径，默认为 config/persistence_config.yaml

        Returns:
            CleanupConfig实例
        """
        if config_path is None:
            from app.settings import settings

            config_path = settings.config_dir / "persistence_config.yaml"

        config_file = Path(config_path)
        if not config_file.exists():
            # 文件不存在时使用默认配置
            logger.warning("✗ 配置文件不存在: %s，使用默认配置", config_path)
            config = cls()
        else:
            try:
                with open(config_file, "r", encoding="utf-8") as f:
                    config_dict = yaml.safe_load(f) or {}
                logger.info("✓ 已加载cleanup配置: %s", config_path)
                config = cls.from_dict(config_dict)
            except Exception as e:
                logger.error("✗ 加载配置文件失败: %s，使用默认配置", e, exc_info=True)
                config = cls()

        config._log_loaded_config()
        return config

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "CleanupConfig":
        """从整份 yaml 字典取 `storage` 段构造。

        不做字段过滤——真出未知字段就让它响亮地崩，别静默吞。
        """
        return cls(**config_dict.get("storage", {}))

    @property
    def storage_base_dir(self) -> Path:
        """扫描根（绝对路径）——委托 settings 单一真源。"""
        from app.settings import settings

        return settings.storage_base_dir

    def _log_loaded_config(self):
        """输出加载的配置（启动时显示）"""
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("========== Cleanup配置 ==========")
            logger.debug("存储: base_dir=%s", self.storage_base_dir)
            logger.debug(
                "清理: enabled=%s, days=%d",
                self.enable_cleanup,
                self.cleanup_days,
            )
            logger.debug("=================================")


# 全局单例（延迟加载）
_global_cleanup_config: Optional[CleanupConfig] = None


def get_cleanup_config() -> CleanupConfig:
    """获取全局清理配置（单例模式）"""
    global _global_cleanup_config
    if _global_cleanup_config is None:
        _global_cleanup_config = CleanupConfig.from_yaml()
    return _global_cleanup_config
