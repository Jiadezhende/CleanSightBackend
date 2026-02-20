"""
基础模型模块

提供检测和动作分析的基础实现，以及推理架构的核心组件
"""

from .detection import detect_keypoints
from .motion import analyze_motion
from .detection_strategy import DetectionStrategy, YOLOStrategy, TransformerStrategy, StrategyFactory
from .output_adapter import OutputAdapter, YOLOAdapter, TransformerAdapter, AdapterFactory

__all__ = [
    # 原有功能
    "detect_keypoints",
    "analyze_motion",
    # 新架构组件
    "DetectionStrategy",
    "YOLOStrategy",
    "TransformerStrategy",
    "StrategyFactory",
    "OutputAdapter",
    "YOLOAdapter",
    "TransformerAdapter",
    "AdapterFactory",
]

