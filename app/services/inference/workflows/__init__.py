"""
推理工作流子模块

Detector（无状态，推理线程 + 可视化线程）：
    BubbleDetector, BendingDetector, CleanToolDetector, MockDetector

TemporalAnalyzer（有状态，时序线程，每 Client 独立实例）：
    BirthRateAnalyzer, DebounceAnalyzer, MSTCNPhaseAnalyzer, MockAnalyzer
"""

from .detector import Detector, YOLODetector
from .analyzer import TemporalAnalyzer
from .bubble import BubbleDetector, BirthRateAnalyzer
from .bending import BendingDetector, DebounceAnalyzer
from .clean_tool import CleanToolDetector
from .mstcn_phase import MSTCNPhaseAnalyzer
from .mock import MockDetector, MockAnalyzer

__all__ = [
    "Detector",
    "YOLODetector",
    "TemporalAnalyzer",
    "BubbleDetector",
    "BirthRateAnalyzer",
    "BendingDetector",
    "DebounceAnalyzer",
    "CleanToolDetector",
    "MSTCNPhaseAnalyzer",
    "MockDetector",
    "MockAnalyzer",
]
