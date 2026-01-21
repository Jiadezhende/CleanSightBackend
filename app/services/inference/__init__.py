"""推理服务模块。

提供多客户端、多模型并行推理框架，支持：
- Stage-Aware 调度
- CUDA Stream 并行
- 批处理优化
- ClientManager 集成

主要组件：
- StageAwareDispatcher: 按阶段分组调度
- MultiModelWorkerPool: 多模型并行推理
- ModelWorkerService: 统一服务管理
"""

from app.services.inference.dispatcher import StageAwareDispatcher
from app.services.inference.models import InferenceRequest, InferenceResult
from app.services.inference.service import ModelWorkerService
from app.services.inference.worker_pool import MultiModelWorkerPool

# 便捷工厂函数
from app.services.inference.factory import (
    create_model_worker_service_example,
    create_model_worker_service_from_manager,
)

__all__ = [
    # 核心类
    "StageAwareDispatcher",
    "MultiModelWorkerPool",
    "ModelWorkerService",
    # 数据模型
    "InferenceRequest",
    "InferenceResult",
    # 工厂函数
    "create_model_worker_service_from_manager",
    "create_model_worker_service_example",
]
