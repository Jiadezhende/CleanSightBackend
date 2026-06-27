"""
推理工作流子模块（流处理框架：流源 Detector / 流算子 Operator）

Detector（流源，无状态，推理线程 + 可视化线程）：
    BubbleDetector, BendingDetector, MockDetector, CleanLargeDetector, CleanSmallDetector

Operator（流算子，有状态，时序线程，每 Client 独立实例，analyze 推进状态 + judge 出告警）：
    BubbleOperator, BendingOperator, MockOperator
"""

from .detector import Detector, YOLODetector
from .operator import Operator, AlignedFrame
from .bubble import BubbleDetector, BubbleOperator
from .bending import BendingDetector, BendingOperator
from .mock import MockDetector, MockOperator

__all__ = [
    "Detector",
    "YOLODetector",
    "Operator",
    "AlignedFrame",
    "BubbleDetector",
    "BubbleOperator",
    "BendingDetector",
    "BendingOperator",
    "MockDetector",
    "MockOperator",
]
