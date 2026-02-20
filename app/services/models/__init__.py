"""
AI 模型模块

提供面向业务的模型实现，包括：
- 基础检测和动作分析
- 气泡检测
- 弯折检测
"""

# 基础模型
from .base import analyze_motion, detect_keypoints

# 弯折检测
from .bending import EndoscopeBendingDetectionTask, EndoscopeBendingDetector

# 气泡检测
from .bubble import BubbleDetectionTask, BubbleDetector

__all__ = [
    # 基础
    "detect_keypoints",
    "analyze_motion",
    # 气泡检测
    "BubbleDetector",
    "BubbleDetectionTask",
    # 弯折检测
    "EndoscopeBendingDetector",
    "EndoscopeBendingDetectionTask",
]
