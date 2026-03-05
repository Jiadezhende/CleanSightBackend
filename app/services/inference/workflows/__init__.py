"""
推理工作流子模块

包含工作流基类、检测器基础组件和具体检测任务实现
"""

from .infer_workflow import InferenceWorkflow, YOLOWorkflow
from .bending import EndoscopeBendingDetectionTask
from .bubble import BubbleDetectionTask
from .mock import MockDetectionTask

__all__ = [
    "InferenceWorkflow",
    "YOLOWorkflow",
    "EndoscopeBendingDetectionTask",
    "BubbleDetectionTask",
    "MockDetectionTask",
]
