"""
基础模型模块

提供检测和动作分析的基础实现
"""

from .detection import detect_keypoints
from .motion import analyze_motion

__all__ = [
    "detect_keypoints",
    "analyze_motion",
]
