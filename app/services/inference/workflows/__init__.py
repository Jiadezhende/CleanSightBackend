"""
推理工作流子模块（分层：L1 检测 / L3 时序分析 / L4 规则）

Detector（L1，无状态，推理线程 + 可视化线程）：
    BubbleDetector, BendingDetector, MockDetector

TemporalAnalyzer（L3，有状态，时序线程，每 Client 独立实例，只产事实）：
    BubbleAnalyzer, BendingAnalyzer, MockAnalyzer

Judge（L4，有状态，时序线程，每 Client 独立实例，消费事实出告警）：
    BubbleJudge, BendingJudge, MockJudge
"""

from .detector import Detector, YOLODetector
from .analyzer import TemporalAnalyzer
from .judge import Judge
from .bubble import BubbleDetector, BubbleAnalyzer, BubbleJudge
from .bending import BendingDetector, BendingAnalyzer, BendingJudge
from .mock import MockDetector, MockAnalyzer, MockJudge

__all__ = [
    "Detector",
    "YOLODetector",
    "TemporalAnalyzer",
    "Judge",
    "BubbleDetector",
    "BubbleAnalyzer",
    "BubbleJudge",
    "BendingDetector",
    "BendingAnalyzer",
    "BendingJudge",
    "MockDetector",
    "MockAnalyzer",
    "MockJudge",
]
