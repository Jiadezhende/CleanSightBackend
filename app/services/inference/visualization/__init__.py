"""可视化层 (Viz)。

worker.py：VisualizationWorker —— 定时拉取快照 + 渲染循环
pool.py  ：VisualizationWorkerPool —— 线程启停管理
visualizer.py：FixedVisualizer —— 固定渲染器（纯渲染，无线程/队列）
"""

from .visualizer import FixedVisualizer
from .worker import VisualizationWorker
from .pool import VisualizationWorkerPool

__all__ = [
    "VisualizationWorkerPool",
    "VisualizationWorker",
    "FixedVisualizer",
]
