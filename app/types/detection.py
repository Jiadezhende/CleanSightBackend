"""检测契约（L1 检测层产出）。

三个粒度各一个名词：

    DetBox           一个框
    DetectorOutput   一个检测器 × 一帧：全部框 + 元数据 / 成败
    FrameDetection   所有检测器 × 一帧：ts + {流名: DetectorOutput} + 帧分辨率
"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class DetBox:
    """一个检测框（标准格式）。所有检测模型的输出都应转换为此格式。"""

    bbox: List[int]  # [x1, y1, x2, y2]
    confidence: float  # 置信度 [0.0-1.0]
    class_id: int  # 类别 ID
    class_name: str  # 类别名称
    extra: Dict[str, Any] = field(default_factory=dict)  # 单框派生量（不落盘）


@dataclass
class DetectorOutput:
    """一个检测器在一帧上的输出：全部框 + 元数据；推理失败时 success=False、框为空。"""

    boxes: List[DetBox]  # 检测框列表
    metadata: Dict[str, Any]  # 元数据（如模型名称、推理时间等）
    timestamp: float  # 时间戳
    success: bool = True  # 推理是否成功
    error: Optional[str] = None  # 错误信息（失败时提供）


@dataclass
class FrameDetection:
    """一帧多流对齐的检测结果：ts + {流名: DetectorOutput}。

    推理 collector 组装、写回口分发给帧窗 / 最新快照 / 落盘缓冲；offline 回放重建。
    是检测层（L1）产出，与时序层（L3）的 `TemporalEvent` / `TemporalSegment` 同级；特征由下游算子自行从它算出。
    """

    ts: float  # 帧捕获时间戳（= 各流 DetectorOutput.timestamp）
    by_source: Dict[str, DetectorOutput]  # {流名(detector.name): 该流当帧检测}
    # 帧分辨率：fan-out 前定死的每帧常量（同帧各流同值），由 pool 从原始帧盖章带入；
    # 供归一化/空间还原按真实尺寸换算。拆两字段（非 (w,h) 元组）避免隐式序混淆。缺省 None → 走默认兜底。
    frame_width: Optional[int] = None
    frame_height: Optional[int] = None
    # 写回路由句柄（ClientQueues，domain 不依赖 services 故标 Any）：只在 collector → 写回口
    # 这一段有值，写回口取走后置 None——留存下来的帧（帧窗 / 快照 / 落盘 / 离线）一律不带。
    cq: Optional[Any] = field(default=None, repr=False, compare=False)
