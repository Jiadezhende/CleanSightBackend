"""recording 服务配置（`config/recording_config.yaml`）。

两个旋钮，故是一个扁平 dataclass —— persistence 那边的三层嵌套是因为它有 storage / hls /
alarm 三组参数，这里没有分组可言。

**刻意没有 `workers`**：落盘走的 `SerialTaskQueue` 恒为一个消费线程，「同一 step 的写按提交序执行」
这条保证就建立在这上面（见 `service.py` 的并发一节）。把它配出来只会诱导人去改大，而改大
之后失效的是回放的正确性、不是吞吐——不报错，只是段间 tfdt 开始碰撞。
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

logger = logging.getLogger(__name__)

_CONFIG_FILENAME = "recording_config.yaml"


@dataclass
class RecordingConfig:
    """录制落盘配置。"""

    # 队列排队上限。满了 `submit_segment` 返回 False 并告警——**满队列是要被看见的**，
    # 所以不给无界选项：无界只是把"丢一段录像"换成"吃光内存"。
    queue_size: int = 100

    # 从活跃 CQ 拉整段的扫描间隔（秒）。1s ≪ 段周期(≈10s) 且 ≪ CQ 缓冲容量(≈90s)。
    sweep_interval_seconds: float = 1.0

    @classmethod
    def from_yaml(cls, config_path: Optional[str] = None) -> "RecordingConfig":
        """从 YAML 加载；文件不存在或解析失败时用默认值（记日志，不抛）。"""
        if config_path is None:
            from app.settings import settings

            config_path = settings.config_dir / _CONFIG_FILENAME

        config_file = Path(config_path)
        if not config_file.exists():
            logger.warning("✗ 配置文件不存在: %s，使用默认配置", config_path)
            return cls()
        try:
            with open(config_file, "r", encoding="utf-8") as f:
                raw: Dict[str, Any] = yaml.safe_load(f) or {}
        except Exception as e:
            logger.error("✗ 加载配置文件失败: %s，使用默认配置", e, exc_info=True)
            return cls()

        logger.info("✓ 已加载 recording 配置: %s", config_path)
        # yaml 由 git 跟踪、每次部署整仓覆盖为干净版，磁盘不会残留已废字段；
        # 故不做字段过滤——真出未知字段就让它响亮地崩，别静默吞（同 persistence 口径）。
        return cls(**raw)


_global_recording_config: Optional[RecordingConfig] = None


def get_recording_config() -> RecordingConfig:
    """全局配置单例（延迟加载）。"""
    global _global_recording_config
    if _global_recording_config is None:
        _global_recording_config = RecordingConfig.from_yaml()
    return _global_recording_config
