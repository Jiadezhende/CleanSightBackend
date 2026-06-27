"""推理请求和结果的数据模型（客户端/Stage 级别）

数据模型层次：
- data_models.py: Task 级别 - 单个检测任务的数据结构（DetectionOutput 等）
- 本模块（models.py）: 客户端/Stage 级别 - 汇总多个 Task 的结果

核心数据流（双写 + 原子快照）：
    InferenceRequest (客户端请求)
      ↓
    InferenceResult (汇总多个 Task 的推理结果)
      → result: Dict[str, DetectionOutput]
      ├─ cq.push_detection()      → slide_window（供 TemporalWorker 历史分析）
      └─ cq.set_latest_inference() → _latest_inference（供 VisualizationWorker 原子读取）

    TemporalWorker (1Hz)  → cq.set_latest_temporal(events)
    VisualizationWorker (~15Hz) → cq.get_latest_inference() + get_latest_frame() + get_latest_temporal()
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict

import numpy as np

from app.models.frame import Frame
from app.services.inference.data_models import (
    ALARM_MODE_REALTIME,
    AlarmMetric,
    FrameDetections,
)


@dataclass
class InferenceRequest:
    """推理请求：帧 + 元数据。"""

    client_id: str
    frame: np.ndarray
    timestamp: float
    stage: str
    frame_data: Frame


@dataclass
class FrameInference:
    """推理结果：汇总多个 Task 的检测输出。

    专门用于传递第一步目标检测后的聚合结果。
    result 字典的每个 value 是单个 Task 的 DetectionOutput。
    """

    client_id: str
    timestamp: float
    stage: str
    result: Dict[str, "FrameDetections"]


@dataclass
class AlarmRecord:
    """告警记录：存储在 ClientQueues.alarm_log（内存环形缓冲区）。

    Attributes:
        alarm_type: 告警类型（如 "流程违规"）
        alarm_level: 告警级别（low/medium/high/critical）
        alarm_message: 告警消息
        timestamp: 告警产生时间
        metadata: 附加信息（如检测计数、窗口比例等）
    """

    alarm_type: str
    alarm_level: str
    alarm_message: str
    mode: str = ALARM_MODE_REALTIME
    metric: str = AlarmMetric.UNKNOWN
    stage: str = ""
    seq: int = 0
    count: int = 1
    timestamp: float = field(default_factory=time.time)
    metadata: Dict[str, Any] = field(default_factory=dict)
