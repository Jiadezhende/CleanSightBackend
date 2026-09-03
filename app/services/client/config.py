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

# 校验阈值：一律用「关系式 / 秒」，不用绝对帧数——本模块货币是时间，绝对帧数阈值的语义会
# 随 raw_fps 漂移（同一个 300 在 30fps 下是 10s、15fps 下是 20s）。
_MIN_SEGMENT_SECONDS = 5.0     # HLS 段时长下限：过短则段数与每段 ffmpeg 固定开销放大
_SEGMENT_HEADROOM_RATIO = 1.2  # ca_maxlen 相对 ca_segment_len 的最小余量（预留 20%）


@dataclass
class FrameConfig:
    """帧处理配置"""

    resize_width: int = 640  # Resize宽度
    resize_height: int = 480  # Resize高度
    # 注意：inference_fps 从 app/settings.py 读取（单一真源）；CA 缓存/段长以秒声明
    # （ca_maxlen_seconds / ca_segment_seconds），帧数在 ca_maxlen/ca_segment_len 属性按 raw_fps 换算


@dataclass
class StateConfig:
    """状态配置"""

    # 注意：初始 stage 不在此配置——未分配任务的客户端默认 MOCK 透传，
    # 由 ClientQueues(stage="MOCK") 构造兜底，无可配语义。
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
            from app.settings import settings

            config_path = settings.config_dir / "client_config.yaml"

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
        """CA队列最大长度（帧数）：时间概念 ca_maxlen_seconds 在此边界按 raw_fps 换算为帧数。"""
        from app.settings import settings
        return settings.ca_maxlen_seconds * settings.raw_fps

    @property
    def ca_segment_len(self) -> int:
        """HLS段长度（帧数）：时间概念 ca_segment_seconds 在此边界按 raw_fps 换算为帧数。"""
        from app.settings import settings
        return settings.ca_segment_seconds * settings.raw_fps

    @property
    def inference_decimation(self) -> int:
        """检测抽帧降采样倍率（从 settings 单一真源读取；抽帧器「每 N 帧留 1」）"""
        from app.settings import settings
        return settings.inference_decimation

    def cq_kwargs(self) -> Dict[str, Any]:
        """组装 ClientQueues 构造参数（resize 属 client 配置，采样倍率/队列走 settings 单一真源）。

        创建 CQ 的唯一配置出口：run 起始由 InferenceManager 调用（早于起流），
        避免"裸建默认值 + 起流时 kwargs 被丢弃"的 dead-kwargs 问题。
        """
        return {
            "resize_width": self.frame.resize_width,
            "resize_height": self.frame.resize_height,
            "inference_decimation": self.inference_decimation,
            "ca_maxlen": self.ca_maxlen,
            "ca_segment_len": self.ca_segment_len,
        }

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
                "帧处理: %dx%d, 抽帧倍率 N=%d（每 N 帧留 1）",
                self.frame.resize_width,
                self.frame.resize_height,
                self.inference_decimation,
            )
            logger.debug(
                "状态: timeout=%ds",
                self.state.heartbeat_timeout,
            )
            logger.debug("📌 队列/fps参数来源: app/settings.py (单一真源)")
            logger.debug("==================================")

    def _validate_config(self):
        """配置验证：只查关系不变式，不查绝对帧数。

        原 `ca_maxlen < 300` 的绝对帧数地板已删——那是秒制重构前的遗留，冻在帧空间里，
        语义随 raw_fps 漂移（30fps 下意味 10s、15fps 下意味 20s），且其数值与
        ca_segment_len 相等纯属当前配置的巧合。真正该守的是下面两条关系式。
        """
        from app.settings import settings

        warnings = []

        # 1. HLS 段过短：段数与每段 ffmpeg 固定开销随之放大。段长是时间概念，按秒判、与 raw_fps 无关。
        if settings.ca_segment_seconds < _MIN_SEGMENT_SECONDS:
            warnings.append(
                f"⚠️  HLS段过短: ca_segment_seconds={settings.ca_segment_seconds}s "
                f"（建议>={_MIN_SEGMENT_SECONDS}s），段数与每段 ffmpeg 固定开销会放大"
            )

        # 2. 装不下一个段 → 永远触发不了分段（致命）
        if self.ca_segment_len > self.ca_maxlen:
            warnings.append(
                f"❌ ca_segment_len({self.ca_segment_len}) > ca_maxlen({self.ca_maxlen})，"
                f"会导致永远无法触发分段"
            )
        # 3. 装得下但没余量：分段消费一抖动就丢帧，而录制腿丢帧 = 录像永久空洞（非仅延迟）
        elif self.ca_maxlen < self.ca_segment_len * _SEGMENT_HEADROOM_RATIO:
            warnings.append(
                f"⚠️  CA队列余量不足: ca_maxlen({self.ca_maxlen}) < "
                f"ca_segment_len({self.ca_segment_len})×{_SEGMENT_HEADROOM_RATIO}"
                f"（需预留{(_SEGMENT_HEADROOM_RATIO - 1) * 100:.0f}%余量），"
                f"分段消费抖动即丢帧 → 录像空洞"
            )

        # 注：原 client.inference_fps ↔ settings.inference_fps 冲突检查已删——
        # 采样倍率现单一真源 settings.inference_decimation，client 直读同源，无从冲突。

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
