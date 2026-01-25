"""AI 推理模块 - 对外接口

基于推理服务架构改进方案（INFERENCE_SERVICE_IMPROVEMENT_PLAN.md）的完整实现。

本模块提供统一的对外接口，内部实现已重构到 inference 子模块：
- InferenceManager → inference.core.manager
- DefaultVisualizer → inference.components.visualizer
- 持久化逻辑 → inference.persistence (待提取)
"""

from typing import Any, Dict, Optional

import numpy as np

from app.models.frame import FrameData, ProcessedFrame
from app.models.task import Task as CleaningTask
from app.services.inference.core.manager import InferenceManager
from app.settings import settings

# ========== 模块级单例（兼容旧代码） ==========

manager = InferenceManager(
    use_async_pipeline=True,
    ca_segment_seconds=settings.ca_segment_seconds,
)


def start():
    """启动推理服务"""
    manager.start()


def stop():
    """停止推理服务"""
    manager.stop()


def set_stream_url(client_id: str, stream_url: str):
    """设置客户端的通用流地址（RTMP/RTSP）"""
    manager.set_stream_url(client_id, stream_url)


def get_result(client_id: str, as_model: bool = False):
    """获取推理结果"""
    return manager.get_result(client_id, as_model=as_model)


def remove_client(client_id: str):
    """移除客户端（包含优雅停止流程）"""
    manager.remove_client(client_id)


def status():
    """获取服务状态"""
    return manager.status()


def set_task(client_id: str, task: Optional[CleaningTask]) -> bool:
    """为客户端设置任务"""
    return manager.set_task(client_id, task)


def get_task(client_id: str) -> Optional[CleaningTask]:
    """获取客户端的任务"""
    return manager.get_task(client_id)


def report_alarm(alarm_info: Dict[str, Any]):
    """上报告警信息（外部调用接口）

    Args:
        alarm_info: 告警信息字典，应包含:
            - task_id: 任务ID
            - step_id: 步骤ID
            - client_id: 客户端ID
            - detection_result: 检测结果（可选）
            - alarm_type: 告警类型（可选，默认根据detection_result判断）
            - alarm_level: 告警级别（可选，默认'high'）
            - alarm_message: 告警消息（可选，默认根据detection_result生成）

    示例:
        >>> from app.services import ai
        >>> ai.report_alarm({
        ...     'task_id': 123,
        ...     'step_id': 1,
        ...     'client_id': 'client_001',
        ...     'alarm_type': '流程违规',
        ...     'alarm_message': '检测到气泡',
        ...     'detection_result': {'bubble_detected': True}
        ... })
    """
    manager.enqueue_alarm(alarm_info)
