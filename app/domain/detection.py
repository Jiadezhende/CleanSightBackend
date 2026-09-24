"""检测契约（L1 检测层产出）。

三个粒度各一个名词：

    DetBox           一个框
    DetectorOutput   一个检测器 × 一帧：全部框 + 元数据 / 成败
    FrameDetection   所有检测器 × 一帧：ts + {流名: DetectorOutput} + 帧分辨率
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import numpy as np


@dataclass
class DetBox:
    """一个检测框（标准格式）。所有检测模型的输出都应转换为此格式。"""

    bbox: List[int]  # [x1, y1, x2, y2]
    confidence: float  # 置信度 [0.0-1.0]
    class_id: int  # 类别 ID
    class_name: str  # 类别名称
    mask: Optional[np.ndarray] = None  # 分割掩码（可选）
    keypoints: Optional[List] = None  # 关键点（可选）
    extra: Dict[str, Any] = field(default_factory=dict)  # 扩展数据


@dataclass
class DetectorOutput:
    """一个检测器在一帧上的输出：全部框 + 元数据；推理失败时 success=False、框为空。"""

    detections: List[DetBox]  # 检测结果列表
    metadata: Dict[str, Any]  # 元数据（如模型名称、推理时间等）
    timestamp: float  # 时间戳
    success: bool = True  # 推理是否成功
    error: Optional[str] = None  # 错误信息（失败时提供）


@dataclass
class FrameDetection:
    """一帧多流对齐的检测结果：ts + {流名: DetectorOutput}。

    online 写回口物化、offline 回放重建；不含 cq，可跨 client/inference/offline 复用。
    是检测层（L1）产出，与时序层（L3）的 `Fact` 同级；特征由下游算子自行从它算出。
    """

    ts: float  # 帧捕获时间戳（= 各流 DetectorOutput.timestamp）
    by_source: Dict[str, DetectorOutput]  # {流名(detector.name): 该流当帧检测}
    # 帧分辨率：fan-out 前定死的每帧常量（同帧各流同值），由 pool 从原始帧盖章、写回口物化带入；
    # 供归一化/空间还原按真实尺寸换算。拆两字段（非 (w,h) 元组）避免隐式序混淆。缺省 None → 走默认兜底。
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None
