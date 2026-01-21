"""推理请求和结果的数据模型。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

import numpy as np

from app.models.frame import FrameData


@dataclass
class InferenceRequest:
    """推理请求：帧 + 元数据。

    Attributes:
        client_id: 客户端标识
        frame: 原始帧（NumPy 数组）
        timestamp: 时间戳
        stage: 当前阶段（LEAK/CLEAN/etc.）
        frame_data: 原始 FrameData（用于回写）
    """

    client_id: str
    frame: np.ndarray
    timestamp: float
    stage: str
    frame_data: FrameData


@dataclass
class InferenceResult:
    """推理结果：关联客户端。

    Attributes:
        client_id: 客户端标识
        timestamp: 时间戳
        stage: 当前阶段
        result: 子任务推理结果字典 {subtask_name: result_dict}
        annotated_frame: 可视化后的帧（可选）
        frame: 推理时使用的原始帧（用于可视化）
    """

    client_id: str
    timestamp: float
    stage: str
    result: Dict[str, Any]  # subtask_results
    annotated_frame: Optional[np.ndarray] = None
    frame: Optional[np.ndarray] = None  # 新增：推理时的原始帧


@dataclass
class TemporalAnalysisResult:
    """时序分析结果。

    Attributes:
        client_id: 客户端标识
        timestamp: 时间戳
        stage_changed: 是否切换stage
        new_stage: 新的stage（如果切换）
        step_completed: 当前步骤是否完成
        events: 触发的事件列表
        state_snapshot: ClientState 快照
    """

    client_id: str
    timestamp: float
    stage_changed: bool
    new_stage: Optional[str]
    step_completed: bool
    events: list[str]
    state_snapshot: Dict[str, Any]


@dataclass
class FrontendMessage:
    """返回给前端的消息（精简版）。

    Attributes:
        client_id: 客户端标识
        timestamp: 时间戳
        stage: 当前阶段
        detections: 检测结果（布尔值）
        confidences: 置信度
        status_message: 状态提示
        progress: 阶段进度
    """

    client_id: str
    timestamp: float
    stage: str
    detections: Dict[str, bool]  # {"bubble": True, "bending": False}
    confidences: Dict[str, float]  # {"bubble": 0.95, "bending": 0.23}
    status_message: str  # "检测到气泡 (连续3帧)"
    progress: Dict[str, Any]  # {"current_step": "气泡检测", "completed": False}


@dataclass
class TemporalAnalysisPackage:
    """时序分析后的数据包（传递给可视化线程）。

    Attributes:
        client_id: 客户端标识
        timestamp: 时间戳
        stage: 当前阶段
        inference_result: 推理结果
        temporal_result: 时序分析结果
        frontend_message: 前端消息
        raw_frame: 原始帧（供可视化使用）
    """

    client_id: str
    timestamp: float
    stage: str
    inference_result: Dict[str, Any]
    temporal_result: TemporalAnalysisResult
    frontend_message: FrontendMessage
    raw_frame: np.ndarray


@dataclass
class WriteBackData:
    """完整的写回数据包（最终形态）。

    Attributes:
        client_id: 客户端标识
        timestamp: 时间戳
        stage: 当前阶段
        processed_frame: 处理后的帧（可视化后）
        inference_result: 推理结果JSON（完整结构化数据）
        frontend_message: 返回给前端的消息
        temporal_result: 时序分析结果（可选，用于调试）
    """

    client_id: str
    timestamp: float
    stage: str
    processed_frame: np.ndarray
    inference_result: Dict[str, Any]
    frontend_message: FrontendMessage
    temporal_result: Optional[TemporalAnalysisResult] = None
