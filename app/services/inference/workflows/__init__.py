"""推理检测任务实现（可插拔，保持内聚：Detector + Operator 同文件）。

抽象基类已上移分层包：
    流源 Detector / YOLODetector → app.services.inference.detection.detector
    流算子 Operator / AlignedFrame → app.services.inference.temporal.operator

本目录只放具体检测任务（一任务一文件）：
    Detector（流源，无状态）：BubbleDetector, BendingDetector, MockDetector,
        CleanLargeDetector, CleanSmallDetector
    Operator（流算子，有状态，analyze 推进状态 + judge 出告警）：
        BubbleOperator, BendingOperator, MockOperator
"""

from .bubble import BubbleDetector, BubbleOperator
from .bending import BendingDetector, BendingOperator
from .mock import MockDetector, MockOperator
from .clean import CleanLargeDetector, CleanSmallDetector

__all__ = [
    "BubbleDetector",
    "BubbleOperator",
    "BendingDetector",
    "BendingOperator",
    "MockDetector",
    "MockOperator",
    "CleanLargeDetector",
    "CleanSmallDetector",
]
