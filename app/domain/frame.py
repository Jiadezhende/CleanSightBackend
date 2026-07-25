"""帧载体契约。"""

from dataclasses import dataclass

import numpy as np


@dataclass
class Frame:
    """轻量级帧数据类，用于内存队列传递（避免 Base64 编码开销）。

    ca_raw / ca_processed / ca_ready 三队列 + latest_rendered 的载体：
    bundle 时间戳与像素，使二者随队列同行。
    """

    timestamp: float  # Unix timestamp
    frame: np.ndarray  # 原始或处理后的帧（numpy 数组）
