"""
气泡检测模块

提供气泡检测模型和推理任务
"""

from .detector import BubbleDetector
from .task import BubbleDetectionTask

__all__ = [
    "BubbleDetector",
    "BubbleDetectionTask",
]
