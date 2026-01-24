"""
推理服务模块

提供统一的推理、时序分析、可视化和持久化接口
"""

# 核心模块
from .core import (
    InferenceManager,
    ModelWorkerService,
    StageAwareDispatcher,
    create_model_worker_service_from_manager,
)

# Worker 池
from .workers import (
    MultiModelWorkerPool,
    TemporalWorkerPool,
    VisualizationWorkerPool,
    WriteBackWorkerPool,
)

# 组件
from .components import (
    DefaultVisualizer,
    TemporalAnalyzer,
    DefaultTemporalAnalyzer,
    ComponentFactory,
)

# 数据模型
from .models import (
    InferenceResult,
    TemporalAnalysisResult,
    TemporalAnalysisPackage,
    WriteBackData,
    FrontendMessage,
)

# 配置加载器
from .config_loader import load_stage_config, StageConfig

__all__ = [
    # 核心
    "InferenceManager",
    "ModelWorkerService",
    "StageAwareDispatcher",
    "create_model_worker_service_from_manager",
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
