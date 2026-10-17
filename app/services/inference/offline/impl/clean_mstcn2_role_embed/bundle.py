"""bundle 特征（94 列）：各类检测框的位置、面积、速度，器械轴坐标，手与器械的相对位置。

    bundle = compute_bundle(frame_rows)      # bundle.columns：{列名: [T]}；features / active / frame 为中间量
    cols = bundle_columns(frame_rows)

列与训练仓 ``tools/extract_ama_features.py::build_candidate_columns``（base 83 + axis 2 + pair 9）逐位一致，
计算见 ``features_ama/clean_bbox_v5.build_bundle``。role-embed 只用其中 89 列，另 26 列手槽位列由 ``slots.py`` 按握镜手 / 送刷手重算（复用这里的中间量）。
"""

from __future__ import annotations

import numpy as np

from .features_ama.clean_bbox_v5 import Bundle, build_bundle
from .frames import CLASS_NAMES, FPS

CONFIDENCE_DEFAULT = 1.0  # 框文件不带置信度列，训练时按 1.0 计


def compute_bundle(frame_rows: list[list[tuple]]) -> Bundle:
    return build_bundle(frame_rows, detection_mapping=CLASS_NAMES, fps=FPS, confidence_default=CONFIDENCE_DEFAULT)


def bundle_columns(frame_rows: list[list[tuple]]) -> dict[str, np.ndarray]:
    """单视频 bundle 94 列：{列名: [T]}（float32）。"""
    return compute_bundle(frame_rows).columns
