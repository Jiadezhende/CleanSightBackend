"""
持久化配置模型（重构版）

支持从YAML文件加载配置，提供默认值
"""

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)


@dataclass
class HLSConfig:
    """HLS持久化配置"""

    workers: int = 2
    queue_size: int = 100
    segment_duration: int = 10
    # 注意：raw_fps和processed_fps从inference config动态获取，不在此定义


@dataclass
class AlarmConfig:
    """告警持久化配置"""

    workers: int = 1
    queue_size: int = 200


@dataclass
class StorageConfig:
    """存储配置"""

    base_dir: str = "./database"
    enable_db_write: bool = False
    enable_cleanup: bool = False
    cleanup_days: int = 7
    cleanup_interval_seconds: int = 3600


@dataclass
class PersistenceConfig:
    """持久化配置（统一入口）"""

    storage: StorageConfig = field(default_factory=StorageConfig)
    hls: HLSConfig = field(default_factory=HLSConfig)
    alarm: AlarmConfig = field(default_factory=AlarmConfig)

    @classmethod
    def from_yaml(cls, config_path: Optional[str] = None) -> "PersistenceConfig":
        """从YAML配置文件加载

        Args:
            config_path: YAML配置文件路径，默认为 config/persistence_config.yaml

        Returns:
            PersistenceConfig实例
        """
        if config_path is None:
            base_dir = Path(__file__).parent.parent.parent.parent.resolve()
            config_path = base_dir / "config" / "persistence_config.yaml"

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
                logger.info("✓ 已加载persistence配置: %s", config_path)
                config = cls.from_dict(config_dict)
            except Exception as e:
                logger.error("✗ 加载配置文件失败: %s，使用默认配置", e, exc_info=True)
                config = cls()

        # 从inference config读取共享参数（fps）
        config._load_shared_params_from_inference()

        # 输出配置日志和验证
        config._log_loaded_config()
        config._validate_config()

        return config

    @classmethod
    def from_dict(cls, config_dict: Dict[str, Any]) -> "PersistenceConfig":
        """从字典构造配置对象

        Args:
            config_dict: 配置字典

        Returns:
            PersistenceConfig实例
        """
        storage = StorageConfig(**config_dict.get("storage", {}))
        hls = HLSConfig(**config_dict.get("hls", {}))
        alarm = AlarmConfig(**config_dict.get("alarm", {}))
        return cls(storage=storage, hls=hls, alarm=alarm)

    @property
    def storage_base_dir(self) -> Path:
        """存储根目录（绝对路径）。

        相对路径以项目根为基（与 segment_finder.get_default_base_dir 对齐），
        避免读写两侧因进程 cwd 不同而分叉到不同目录。
        """
        p = Path(self.storage.base_dir)
        if p.is_absolute():
            return p.resolve()
        project_root = Path(__file__).parent.parent.parent.parent.resolve()
        return (project_root / p).resolve()

    # 向后兼容属性
    @property
    def hls_workers(self) -> int:
        return self.hls.workers

    @property
    def hls_queue_size(self) -> int:
        return self.hls.queue_size

    @property
    def segment_duration(self) -> int:
        return self.hls.segment_duration

    @property
    def raw_fps(self) -> float:
        return self.hls.raw_fps

    @property
    def processed_fps(self) -> float:
        return self.hls.processed_fps

    @property
    def alarm_workers(self) -> int:
        return self.alarm.workers

    @property
    def alarm_queue_size(self) -> int:
        return self.alarm.queue_size

    @property
    def enable_db_write(self) -> bool:
        return self.storage.enable_db_write

    @property
    def enable_cleanup(self) -> bool:
        return self.storage.enable_cleanup

    @property
    def cleanup_days(self) -> int:
        return self.storage.cleanup_days

    @property
    def cleanup_interval_seconds(self) -> int:
        return self.storage.cleanup_interval_seconds

    def _load_shared_params_from_inference(self):
        """从inference配置加载共享参数（fps等）

        所有跨模块共享的参数统一在 inference_config.yaml 的 global 部分定义
        """
        try:
            from app.services.inference.config import load_stage_config

            inference_config = load_stage_config()

            # 从inference config读取共享参数
            self._raw_fps = inference_config.raw_fps
            self._processed_fps = inference_config.inference_fps

            logger.debug(
                "✓ 已从inference_config.yaml读取共享参数: raw_fps=%.1f, inference_fps=%d",
                self._raw_fps,
                self._processed_fps,
            )
        except Exception as e:
            # 如果无法加载inference配置，使用默认值
            logger.warning("✗ 无法从inference配置读取共享参数，使用默认值: %s", e)
            self._raw_fps = 30.0
            self._processed_fps = 20.0

    @property
    def raw_fps(self) -> float:
        """原始视频帧率（从inference config读取）"""
        return getattr(self, "_raw_fps", 30.0)

    @property
    def processed_fps(self) -> float:
        """处理后视频帧率（从inference config读取）"""
        return getattr(self, "_processed_fps", 20.0)

    def _log_loaded_config(self):
        """输出加载的配置（启动时显示）"""
        # DEBUG级别显示详细配置
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("========== Persistence配置 ==========")
            logger.debug("存储: base_dir=%s", self.storage.base_dir)
            logger.debug(
                "HLS: workers=%d, queue=%d, raw_fps=%.1f, processed_fps=%.1f",
                self.hls.workers,
                self.hls.queue_size,
                self.raw_fps,
                self.processed_fps,
            )
            logger.debug(
                "告警: workers=%d, queue=%d",
                self.alarm.workers,
                self.alarm.queue_size,
            )
            logger.debug(
                "清理: enabled=%s, days=%d",
                self.storage.enable_cleanup,
                self.storage.cleanup_days,
            )
            logger.debug(
                "📌 fps参数来源: inference_config.yaml (global.raw_fps, global.inference_fps)"
            )
            logger.debug("=====================================")

    def _validate_config(self):
        """配置验证和冲突检测"""
        warnings = []

        # 1. 检查processed_fps是否与inference config一致（由inference模块检查）
        # 这里不做检查，避免循环依赖

        # 2. 检查队列容量合理性
        if self.hls.queue_size < 100:
            warnings.append(f"⚠️  HLS队列容量过小: {self.hls.queue_size}，建议>=256")

        if self.alarm.queue_size < 64:
            warnings.append(f"⚠️  告警队列容量过小: {self.alarm.queue_size}，建议>=128")

        # 3. 检查Worker数量合理性
        if self.hls.workers > 4:
            warnings.append(
                f"⚠️  HLS Worker数量过多: {self.hls.workers}，建议2-4（避免CPU竞争）"
            )

        if self.hls.workers < 1:
            warnings.append(f"❌ HLS Worker数量必须>=1")

        if self.alarm.workers < 1:
            warnings.append(f"❌ 告警Worker数量必须>=1")

        # 注意：告警重试配置由 GuardedExecutor 统一处理，无需在此验证

        # 输出警告
        if warnings:
            logger.warning("========== 配置问题检测 ==========")
            for warning in warnings:
                logger.warning(warning)
            logger.warning("=====================================")


# 全局单例（延迟加载）
_global_persistence_config: Optional[PersistenceConfig] = None


def get_persistence_config() -> PersistenceConfig:
    """获取全局持久化配置（单例模式）"""
    global _global_persistence_config
    if _global_persistence_config is None:
        _global_persistence_config = PersistenceConfig.from_yaml()
    return _global_persistence_config
