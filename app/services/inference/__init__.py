"""
推理服务模块

提供统一的推理、时序分析、可视化和持久化接口，以及具体检测任务实现
"""

# Stage 工厂
from .stage_factory import StageFactory

# 配置加载器
from .config import StageConfig, load_stage_config

# 推理工作流 & 检测器 / 分析器 / 规则
from .workflows import (
    Detector,
    YOLODetector,
    TemporalAnalyzer,
    Judge,
    BubbleDetector,
    BubbleAnalyzer,
    BubbleJudge,
    BendingDetector,
    BendingAnalyzer,
    BendingJudge,
    MockDetector,
    MockAnalyzer,
    MockJudge,
)

# 核心模块
from .core import (
    InferenceManager,
    ModelWorkerService,
    StageAwareDispatcher,
)

# 数据模型
from .models import (
    InferenceResult,
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
    "TemporalAnalyzer",
    "Judge",
    # 检测器
    "BubbleDetector",
    "BendingDetector",
    "MockDetector",
    # 时序分析器（L3 只产事实）
    "BubbleAnalyzer",
    "BendingAnalyzer",
    "MockAnalyzer",
    # 规则（L4 出告警）
    "BubbleJudge",
    "BendingJudge",
    "MockJudge",
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
    "InferenceResult",
    # 配置
    "load_stage_config",
    "StageConfig",
]
