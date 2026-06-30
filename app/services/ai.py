"""AI 推理模块 - 对外接口

基于推理服务架构改进方案（INFERENCE_SERVICE_IMPROVEMENT_PLAN.md）的完整实现。

本模块提供统一的对外接口，内部实现已重构到 inference 子模块：
- InferenceManager → inference.manager（经包根 re-export）
- 可视化 → inference.visualization
- 持久化逻辑 → inference.persistence (待提取)
"""

from typing import Optional

import numpy as np

from app.domain.task import CleaningTask
from app.services.inference import InferenceManager
from app.settings import settings

# ========== 模块级单例（兼容旧代码） ==========
# 帧率/队列参数走 settings 单一真源（见 app/settings.py）

manager = InferenceManager(
    rt_fps=settings.raw_fps,
    ca_segment_seconds=int(
        settings.ca_segment_len / settings.raw_fps
    ),  # 帧数转换为秒数
)


def start():
    """启动推理服务"""
    manager.start()


def stop():
    """停止推理服务"""
    manager.stop()


def get_result(client_id: str):
    """获取最新推理帧（domain Frame）；WS 编码在 routers/ai.py 边界完成。"""
    return manager.get_result(client_id)


def remove_client(client_id: str):
    """移除客户端（包含优雅停止流程）"""
    manager.remove_client(client_id)


def status():
    """获取服务状态"""
    return manager.status()


def set_task(client_id: str, task: Optional[CleaningTask]) -> bool:
    """为客户端设置任务"""
    return manager.set_task(client_id, task)



