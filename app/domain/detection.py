"""检测契约（L1 检测层产出）。

检测器（Detector.infer）产出标准化检测结果。Detection 是单个框，
FrameDetections 是一帧里某检测器产出的全部框（亦作推理最终输出格式）。
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class Detection:
    """单个检测结果（标准格式）。所有检测模型的输出都应转换为此格式。"""

    bbox: List[int]  # [x1, y1, x2, y2]
    confidence: float  # 置信度 [0.0-1.0]
    class_id: int  # 类别 ID
    class_name: str  # 类别名称
    mask: Optional[np.ndarray] = None  # 分割掩码（可选）
    keypoints: Optional[List] = None  # 关键点（可选）
    extra: Dict[str, Any] = field(default_factory=dict)  # 扩展数据


@dataclass
class FrameDetections:
    """一帧里某检测器产出的全部检测（标准化输出，亦作推理最终输出格式）。"""

    detections: List[Detection]  # 检测结果列表
    metadata: Dict[str, Any]  # 元数据（如模型名称、推理时间等）
    timestamp: float  # 时间戳
    success: bool = True  # 推理是否成功
    error: Optional[str] = None  # 错误信息（失败时提供）


@dataclass
class FrameFeature:
    """一帧多流对齐的检测记录（特征层输入）：ts + {流名: FrameDetections}。

    online 写回口物化、offline 回放重建；不含 cq，可跨 client/inference/offline 复用。
    注：持有的是对齐后的检测（非计算特征），特征张量化仍在算子内（下一步共享）。
    """

    ts: float  # 帧捕获时间戳（= 各流 FrameDetections.timestamp）
    by_source: Dict[str, FrameDetections]  # {流名(detector.name): 该流当帧检测}
