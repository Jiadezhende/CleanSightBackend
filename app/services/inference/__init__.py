"""
推理服务模块

提供统一的推理、时序分析、可视化和持久化接口，以及具体检测任务实现。

分层（按处理流程）：
    detection/     目标检测层 (L1)：Detector 抽象 + dispatcher/pool/service
    feature/       feature_store 层 (L2)：FeatureStore（在线写）/ FactLedger（离线预留，休眠）
    temporal/      时序分析层 (L3/L4)：Operator 抽象 + actor/alarm_sink
    visualization/ 可视化层：worker/pool/visualizer
    offline/       离线段（预留占位）
    workflows/     可插拔检测任务（Det+Op 内聚单文件）
顶层平铺跨层基础设施：manager / config / naming / stage_factory / models。
"""

# Stage 工厂
from .stage_factory import StageFactory

# 配置加载器
from .config import StageConfig, load_stage_config

# 总编排
from .manager import InferenceManager

# 检测层 (L1)：抽象 + 取帧分组 + 模型池 + worker
from .detection import (
    Detector,
    YOLODetector,
    StageAwareDispatcher,
    MultiModelWorkerPool,
    ModelWorkerService,
)

# 时序分析层 (L3/L4)：抽象 + Actor
from .temporal import (
    Operator,
    ClientTemporalActor,
)

# 可视化层
from .visualization import VisualizationWorkerPool

# 推理检测任务（流源 Detector / 流算子 Operator 具体实现）
from .workflows import (
    BubbleDetector,
    BubbleOperator,
    BendingDetector,
    BendingOperator,
    MockDetector,
    MockOperator,
)

# 数据模型
from .models import (
    FrameInference,
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
