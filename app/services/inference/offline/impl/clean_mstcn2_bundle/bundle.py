"""bundle 特征（94 列）：各类检测框的位置、面积、速度，器械轴坐标，手与器械的相对位置。

列的选取与拼接同仓库 ``tools/extract_ama_features.py::build_candidate_columns``（base 83 + axis 2 + pair 9），
数值由 ``features_ama/clean_bbox_v5.py`` 计算（读框已改为按帧分桶，数值与仓库版逐位一致）。
"""

from __future__ import annotations

import numpy as np

from .features_ama.clean_bbox_v5 import build_ama_v5_features, compact_keep
from .frames import CLASS_NAMES, FPS

CONFIDENCE_DEFAULT = 1.0  # 框文件不带置信度列，训练时按 1.0 计
GROUP_ORDER = ("base", "axis", "pair")


def _group_of(name: str) -> str | None:
    if name in ("scope/axis_observed", "scope/axis_age"):
        return "axis"
    if name.startswith("scope/brush_tip_out_v_along_") or name.startswith("scope/hand_near_v_along_"):
        return None
    if name.startswith("pair/"):
        return "pair"
    if compact_keep(name):
        return "base"
    return None


def bundle_columns(frame_rows: list[list[tuple]]) -> dict[str, np.ndarray]:
    """单视频 bundle 94 列：{列名: [T]}（float32）。"""

    kw = dict(detection_mapping=CLASS_NAMES, fps=FPS, confidence_default=CONFIDENCE_DEFAULT)
    full, full_names, _ = build_ama_v5_features(frame_rows, recipe="full", **kw)
    pair, pair_names, _ = build_ama_v5_features(frame_rows, recipe="fix-compact-pair", **kw)
    groups: dict[str, list[tuple[str, np.ndarray]]] = {g: [] for g in GROUP_ORDER}
    for source, names in ((full, full_names), (pair, pair_names)):
        for index, name in enumerate(names):
            group = _group_of(name)
            # base / axis 取自 full；pair 只取自 fix-compact-pair
            if group is None or (group == "pair") != (source is pair):
                continue
            groups[group].append((name, source[:, index]))
    out = {name: col.astype(np.float32) for g in GROUP_ORDER for name, col in groups[g]}
    if len(out) != 94:
        raise AssertionError(f"bundle 列数 {len(out)} != 94")
    return out
