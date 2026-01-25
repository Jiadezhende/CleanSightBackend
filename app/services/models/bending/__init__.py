"""
弯折检测模块

提供弯折检测模型和推理任务
"""

from .detector import EndoscopeBendingDetector
from .task import EndoscopeBendingDetectionTask

__all__ = [
    "EndoscopeBendingDetector",
    "EndoscopeBendingDetectionTask",
]
