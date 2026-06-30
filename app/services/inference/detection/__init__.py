"""目标检测层 (L1)。

抽象：Detector / YOLODetector（detector.py）
处理流程：StageAwareDispatcher 取帧分组（dispatcher.py）→ MultiModelWorkerPool 模型池（pool.py）
         → ModelWorkerService worker 写回（service.py）
"""

from .detector import Detector, YOLODetector
from .dispatcher import StageAwareDispatcher
from .pool import MultiModelWorkerPool
from .service import ModelWorkerService

__all__ = [
    "Detector",
    "YOLODetector",
    "StageAwareDispatcher",
    "MultiModelWorkerPool",
    "ModelWorkerService",
]
