"""
基础模型模块

提供推理架构的核心组件
"""

from .detector import Detector, YOLODetector, TransformerDetector, StrategyFactory
from .output_adapter import OutputAdapter, YOLOAdapter, TransformerAdapter, AdapterFactory

__all__ = [
    "Detector",
    "YOLODetector",
    "TransformerDetector",
    "StrategyFactory",
    "OutputAdapter",
    "YOLOAdapter",
    "TransformerAdapter",
    "AdapterFactory",
]
