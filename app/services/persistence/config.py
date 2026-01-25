"""
持久化配置模型

支持从settings加载配置，并提供默认值
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from app.settings import settings


@dataclass
class PersistenceConfig:
    """持久化配置"""

    # 存储配置
    storage_base_dir: Path  # 存储根目录

    # HLS持久化配置
    hls_workers: int = 2  # HLS持久化Worker数量
    hls_queue_size: int = 100  # HLS队列最大长度
    segment_duration: int = 10  # 视频段时长（秒）
    raw_fps: float = 30.0  # 原始视频帧率
    processed_fps: float = 20.0  # 处理后视频帧率

    # 告警持久化配置
    alarm_workers: int = 1  # 告警持久化Worker数量
    alarm_queue_size: int = 200  # 告警队列最大长度
    alarm_batch_interval: int = 30  # 批量上报间隔（秒）
    alarm_cooldown_seconds: int = 60  # 告警冷却期（秒）
    alarm_retry_times: int = 3  # 告警上报重试次数
    alarm_retry_backoff: float = 1.0  # 重试退避时间（秒）

    # 数据库配置
    enable_db_write: bool = False  # 是否写入数据库（默认关闭file_path表写入）

    # 清理配置
    enable_cleanup: bool = False  # 是否启用自动清理
    cleanup_days: int = 7  # 保留天数

    @classmethod
    def from_settings(cls) -> 'PersistenceConfig':
        """从全局settings加载配置"""
        import os
        base_dir = Path(__file__).parent.parent.parent.parent.resolve()

        return cls(
            storage_base_dir=base_dir / "database",
            segment_duration=getattr(settings, 'ca_segment_seconds', 10),
            processed_fps=float(getattr(settings, 'inference_fps', 20)),
            alarm_batch_interval=int(os.getenv('CLEANSIGHT_PERSISTENCE__ALARM_BATCH_INTERVAL', '30')),
            alarm_cooldown_seconds=int(os.getenv('CLEANSIGHT_PERSISTENCE__ALARM_COOLDOWN_SECONDS', '60')),
            hls_workers=int(os.getenv('CLEANSIGHT_PERSISTENCE__HLS_WORKERS', '2')),
            hls_queue_size=int(os.getenv('CLEANSIGHT_PERSISTENCE__HLS_QUEUE_SIZE', '100')),
            alarm_workers=int(os.getenv('CLEANSIGHT_PERSISTENCE__ALARM_WORKERS', '1')),
            alarm_queue_size=int(os.getenv('CLEANSIGHT_PERSISTENCE__ALARM_QUEUE_SIZE', '200')),
        )
