"""
推理服务模块

提供统一的推理、时序分析、可视化和持久化接口，以及具体检测任务实现
"""

# 组件
from .components import (
    ComponentFactory,
    DefaultTemporalAnalyzer,
    DefaultVisualizer,
    TemporalAnalyzer,
)

# 配置加载器
from .config import StageConfig, load_stage_config

# 推理工作流 & 检测任务
from .workflows import (
    InferenceWorkflow,
    EndoscopeBendingDetectionTask,
    BubbleDetectionTask,
)

# 核心模块
from .core import (
    InferenceManager,
    ModelWorkerService,
    StageAwareDispatcher,
)

# 数据模型
from .models import (
    FrontendMessage,
    InferenceResult,
    TemporalAnalysisPackage,
    TemporalAnalysisResult,
    WriteBackData,
)

# Worker 池
from .workers import (
    MultiModelWorkerPool,
    TemporalWorkerPool,
    VisualizationWorkerPool,
    WriteBackWorkerPool,
)

__all__ = [
    # 推理工作流基类
    "InferenceWorkflow",
    # 检测任务
    "EndoscopeBendingDetectionTask",
    "BubbleDetectionTask",
    # 核心
    "InferenceManager",
    "ModelWorkerService",
    "StageAwareDispatcher",
    # Worker 池
    "MultiModelWorkerPool",
    "TemporalWorkerPool",
    "VisualizationWorkerPool",
    "WriteBackWorkerPool",
    # 组件
    "DefaultVisualizer",
    "TemporalAnalyzer",
    "DefaultTemporalAnalyzer",
    "ComponentFactory",
    # 数据模型
    "InferenceResult",
    "TemporalAnalysisResult",
    "TemporalAnalysisPackage",
    "WriteBackData",
    "FrontendMessage",
    # 配置
    "load_stage_config",
    "StageConfig",
]
