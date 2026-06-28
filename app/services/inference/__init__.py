"""
推理服务模块

提供统一的推理、时序分析、可视化和持久化接口，以及具体检测任务实现
"""

# Stage 工厂
from .stage_factory import StageFactory

# 配置加载器
from .config import StageConfig, load_stage_config

# 推理工作流 & 流源 Detector / 流算子 Operator
from .workflows import (
    Detector,
    YOLODetector,
    Operator,
    BubbleDetector,
    BubbleOperator,
    BendingDetector,
    BendingOperator,
    MockDetector,
    MockOperator,
)

# 核心模块
from .core import (
    InferenceManager,
    ModelWorkerService,
    StageAwareDispatcher,
)

# 数据模型
from .models import (
    FrameInference,
)

# Worker 池
from .workers import (
    MultiModelWorkerPool,
    ClientTemporalActor,
    VisualizationWorkerPool,
)

__all__ = [
    # 推理基类
    "Detector",
    "YOLODetector",
    "Operator",
    # 流源 Detector
    "BubbleDetector",
    "BendingDetector",
    "MockDetector",
    # 流算子 Operator（analyze 推进状态 + judge 出告警）
    "BubbleOperator",
    "BendingOperator",
    "MockOperator",
    # 核心
    "InferenceManager",
    "ModelWorkerService",
    "StageAwareDispatcher",
    # Worker 池
    "MultiModelWorkerPool",
    "ClientTemporalActor",
    "VisualizationWorkerPool",
    # Stage 工厂
    "StageFactory",
    # 数据模型
    "FrameInference",
    # 配置
    "load_stage_config",
    "StageConfig",
]
