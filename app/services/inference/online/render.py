"""渲染契约（检测器 prepare_visualization_data 产出，供固定渲染器消费）。"""

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

import numpy as np


@dataclass
class RenderSpec:
    """可视化数据：prepare_visualization_data() 返回，供固定渲染器使用。"""

    type: str  # 可视化类型: "bbox" / "mask" / "heatmap" / "keypoint"
    items: List["RenderItem"]  # 可视化项列表
    status_text: str  # 状态栏文本
    status_color: Tuple[int, int, int]  # 状态栏颜色 (B, G, R)
    status_position: str = "top-right"  # top-left / top-right / bottom-left / bottom-right


@dataclass
class RenderItem:
    """单个可视化项（按 RenderSpec.type 决定使用哪些字段）。"""

    bbox: Optional[List[int]] = None  # 边界框 [x1, y1, x2, y2]
    mask: Optional[np.ndarray] = None  # 分割掩码
    heatmap: Optional[np.ndarray] = None  # 热力图
    keypoints: Optional[List] = None  # 关键点列表
    label: str = ""  # 标签文本
    confidence: float = 0.0  # 置信度
    color: Tuple[int, int, int] = (0, 255, 0)  # 颜色 (B, G, R)
    extra: Dict[str, Any] = field(default_factory=dict)  # 扩展数据


class RenderType:
    """可视化类型常量（RenderSpec.type 取值）。"""

    BBOX = "bbox"  # 检测框
    MASK = "mask"  # 分割掩码
    HEATMAP = "heatmap"  # 热力图
    KEYPOINT = "keypoint"  # 关键点
