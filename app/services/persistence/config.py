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
    sweep_interval_seconds: float = 1.0  # HLSSegmentSweeper 扫描间隔（秒，PULL 模型）
    # 注意：不配 segment_duration——分段由 CQ 帧数(ca_segment_len)触发、每段时长由 EXTINF
    #      从帧 ts 自适应，回放侧也从 EXTINF 读；此处配任何时长都是死值、且会误导。
    # 注意：HLS 段编码帧率由 strategy 从帧 ts 自适应反推（eff_fps），不在此配置任何 fps


@dataclass
class AlarmConfig:
    """告警持久化配置"""

    workers: int = 1
    queue_size: int = 200


@dataclass
class StorageConfig:
    """存储配置

    注意：base_dir 已上移到 settings.storage_base_dir（单一真源），不在此定义；
    此处仅保留持久化自有职责的参数（清理策略）。
    """

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
                logger.info("✓ 已加载persistence配置: %s", config_path)
                config = cls.from_dict(config_dict)
            except Exception as e:
                logger.error("✗ 加载配置文件失败: %s，使用默认配置", e, exc_info=True)
                config = cls()

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
        # yaml 由 git 跟踪、每次部署整仓覆盖为干净版，磁盘不会残留已废字段；
        # 故不做字段过滤——真出未知字段就让它响亮地崩，别静默吞。
        storage = StorageConfig(**config_dict.get("storage", {}))
        hls = HLSConfig(**config_dict.get("hls", {}))
        alarm = AlarmConfig(**config_dict.get("alarm", {}))
        return cls(storage=storage, hls=hls, alarm=alarm)

    @property
    def storage_base_dir(self) -> Path:
        """存储根目录（绝对路径）——委托 settings 单一真源。

        历史上各服务各自解析此路径，现统一收敛到 settings.storage_base_dir，
        persistence / inference / traceback 三方同源，消除分叉与跨服务 push。
        """
        from app.settings import settings

        return settings.storage_base_dir

    # 扁平访问器（manager 唯一入口；嵌套 dataclass 仅作分组存储，全仓无嵌套访问）
    @property
    def hls_workers(self) -> int:
        return self.hls.workers

    @property
    def hls_queue_size(self) -> int:
        return self.hls.queue_size

    @property
    def hls_sweep_interval_seconds(self) -> float:
        return self.hls.sweep_interval_seconds

    @property
    def alarm_workers(self) -> int:
        return self.alarm.workers

    @property
    def alarm_queue_size(self) -> int:
        return self.alarm.queue_size

    @property
    def enable_cleanup(self) -> bool:
        return self.storage.enable_cleanup

    @property
    def cleanup_days(self) -> int:
        return self.storage.cleanup_days

    @property
    def cleanup_interval_seconds(self) -> int:
        return self.storage.cleanup_interval_seconds

    def _log_loaded_config(self):
        """输出加载的配置（启动时显示）"""
        # DEBUG级别显示详细配置
        if logger.isEnabledFor(logging.DEBUG):
            logger.debug("========== Persistence配置 ==========")
            logger.debug("存储: base_dir=%s", self.storage_base_dir)
            logger.debug(
                "HLS: workers=%d, queue=%d",
                self.hls.workers,
                self.hls.queue_size,
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
                "📌 HLS 段编码帧率由 strategy 从帧 ts 自适应反推(eff_fps)，配置层不持 fps"
            )
            logger.debug("=====================================")

    def _validate_config(self):
        """配置验证和冲突检测"""
        warnings = []

        # 1. HLS 段编码帧率由 strategy 从帧 ts 反推，配置层不再持 fps，无 fps 一致性校验。

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
