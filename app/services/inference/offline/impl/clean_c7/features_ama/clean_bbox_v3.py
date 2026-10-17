"""CLEAN clean_bbox_v3 scope 坐标系特征（113 维）。

针对 v2 的绝对图像坐标非平稳问题（内镜推拉镜头/平移导致同一动作特征值漂移），
v3 把所有目标位置投影到 scope 器械轴坐标系：

- 轴：``scope_control_body -> scope_distal_end``（缺席回退 ``scope_mid_section``）；
- 位置：``along/across`` = (center - control) 在轴/垂轴上的分量 / 轴长 ``ref_len``；
- 面积：相对 ``scope_distal_end`` 面积的对数比 ``log((area+eps)/(ref_area+eps))``；
- pair 距离天然相对尺度；所有连续列 clip 有界，不依赖全局 z-score。

槽位选择（hand top2 / 其他 top1）与短缺失插值复用 ``clean_bbox_v2`` 的实现，
块布局与 v2 base 一一对应，便于逐块对照消融。详见 docs/FEATURE_INPUT_DESIGN_V3.md。
"""

from __future__ import annotations

import math

import numpy as np

from .clean_bbox_v2 import (
    OBJECTS,
    PAIR_FEATURES,
    _finite,
    _impute_short_gaps,
    _read_object_arrays,
    _select_hand_slots,
    _select_top1_slot,
)

VERSION = "clean_bbox_v3_scope_frame"
CLEAN_V3_VERSION = VERSION  # 与 features/__init__ 导出名一致
FEATURE_DIM = 113
CLEAN_V3_FEATURE_DIMS = {VERSION: FEATURE_DIM}

_CLIP_POS = 4.0
_CLIP_LOG = 6.0
_FALLBACK_REF_LEN = 0.35
_FALLBACK_REF_AREA = 0.02

V3_SLOT_CHANNELS = [
    "present",
    "conf",
    "along",
    "across",
    "log_area_ratio",
    "speed",
    "missing_age",
    "imputed",
]


def _forward_fill(values: np.ndarray, valid: np.ndarray, fallback: float) -> np.ndarray:
    """沿时间前向填充有效值；序列内无任何有效值时全部回退 fallback。"""
    output = np.full(len(values), float(fallback), dtype=np.float32)
    last = None
    for index in range(len(values)):
        if valid[index]:
            last = float(values[index])
        if last is not None:
            output[index] = last
    return output


def _scope_frame(
    features: dict[str, np.ndarray], active: dict[str, np.ndarray], frames: int
):
    """由 scope 三点构造逐帧 (axis, ref_len, ref_area)。

    features[name] 为 ``[T,8]`` v2 插值特征（cx/cy 在 2:4，area 在 4）。
    轴向量依次取 distal-control / distal-mid / mid-control 中当帧有效的一组，
    否则沿用上一帧有效轴，初始回退 (1,0)。ref_len/ref_area 沿有效帧前向填充，
    序列内无有效值时回退中位数 / 常数。
    """
    control = features["scope_control_body"][:, 2:4]
    mid = features["scope_mid_section"][:, 2:4]
    distal = features["scope_distal_end"][:, 2:4]
    active_ctl = active["scope_control_body"]
    active_mid = active["scope_mid_section"]
    active_dis = active["scope_distal_end"]

    axes = np.zeros((frames, 2), dtype=np.float32)
    ref_len = np.full(frames, _FALLBACK_REF_LEN, dtype=np.float32)
    last_axis = None
    valid_lens: list[float] = []
    len_valid = np.zeros(frames, dtype=bool)
    for index in range(frames):
        for (left, right, left_ok, right_ok) in (
            (control[index], distal[index], active_ctl[index], active_dis[index]),
            (mid[index], distal[index], active_mid[index], active_dis[index]),
            (control[index], mid[index], active_ctl[index], active_mid[index]),
        ):
            if not (left_ok and right_ok):
                continue
            vector = right - left
            length = float(np.linalg.norm(vector))
            if length <= 1e-4:
                continue
            last_axis = (vector / length).astype(np.float32)
            ref_len[index] = length
            len_valid[index] = True
            valid_lens.append(length)
            break
        axes[index] = last_axis if last_axis is not None else np.array([1.0, 0.0], np.float32)
    # 轴长：当帧有效用当帧值，否则前向填充；无任何有效值回退常数
    if valid_lens:
        median_len = float(np.median(valid_lens))
        ref_len = np.where(
            len_valid, ref_len, _forward_fill(ref_len, len_valid, median_len)
        ).astype(np.float32)

    area_values = np.where(
        active_dis, features["scope_distal_end"][:, 4], np.nan
    ).astype(np.float32)
    if active_dis.any():
        median_area = float(np.nanmedian(area_values))
        ref_area = np.where(
            active_dis,
            area_values,
            _forward_fill(
                np.nan_to_num(area_values, nan=median_area), active_dis, median_area
            ),
        ).astype(np.float32)
    else:
        ref_area = np.full(frames, _FALLBACK_REF_AREA, dtype=np.float32)
    return axes, ref_len, ref_area


def _slot_channels(feature: np.ndarray, active: np.ndarray, axes, ref_len, ref_area, control):
    """把 v2 插值特征 ``[T,8]`` 转成 v3 的 8 通道 scope 坐标特征。"""
    present = feature[:, 0]
    confidence = feature[:, 1]
    centers = feature[:, 2:4]
    area = feature[:, 4]
    speed = feature[:, 5]
    missing_age = feature[:, 6]
    imputed = feature[:, 7]

    relative = centers - control
    along = np.einsum("td,td->t", relative, axes) / ref_len
    perpendicular = np.stack([-axes[:, 1], axes[:, 0]], axis=1)
    across = np.einsum("td,td->t", relative, perpendicular) / ref_len
    log_ratio = np.log((area + 1e-4) / (ref_area + 1e-4))

    channels = np.stack(
        [
            present,
            confidence,
            np.clip(along, -_CLIP_POS, _CLIP_POS),
            np.clip(across, -_CLIP_POS, _CLIP_POS),
            np.clip(log_ratio, -_CLIP_LOG, _CLIP_LOG),
            speed,
            missing_age,
            imputed,
        ],
        axis=1,
    ).astype(np.float32)
    # 与 v2 一致：缺席帧把连续/置信度通道清零，只保留 present/missing_age/imputed
    channels[~active, 1:6] = 0.0
    return channels


def _build_base_matrix(object_arrays: dict[str, list[np.ndarray]], frames: int, fps: float):
    """构造 v3 base ``[T,113]``，返回矩阵与列名。"""
    features: dict[str, np.ndarray] = {}
    active: dict[str, np.ndarray] = {}
    blocks: list[np.ndarray] = []
    names: list[str] = []

    def slot_names(prefix: str) -> list[str]:
        return [f"{prefix}_{channel}" for channel in V3_SLOT_CHANNELS]

    hand_count, hand_slots = _select_hand_slots(object_arrays.get("hand", []), frames)
    blocks.append((np.clip(hand_count, 0, 3) / 3.0)[:, None].astype(np.float32))
    names.append("hand_count")
    for slot_index, slot in enumerate(hand_slots, start=1):
        feature, slot_active = _impute_short_gaps(slot, fps)
        features[f"hand_top{slot_index}"] = feature
        active[f"hand_top{slot_index}"] = slot_active
    for object_name in OBJECTS:
        if object_name == "hand":
            continue
        count, slot = _select_top1_slot(object_arrays.get(object_name, []), frames)
        feature, object_active = _impute_short_gaps(slot, fps)
        features[object_name] = feature
        active[object_name] = object_active

    axes, ref_len, ref_area = _scope_frame(features, active, frames)
    control = features["scope_control_body"][:, 2:4]

    for slot_index in (1, 2):
        key = f"hand_top{slot_index}"
        blocks.append(_slot_channels(features[key], active[key], axes, ref_len, ref_area, control))
        names.extend(slot_names(f"hand_top{slot_index}"))
    for object_name in OBJECTS:
        if object_name == "hand":
            continue
        count, _slot = _select_top1_slot(object_arrays.get(object_name, []), frames)
        blocks.append(
            np.concatenate(
                [
                    (np.clip(count, 0, 3) / 3.0)[:, None].astype(np.float32),
                    _slot_channels(features[object_name], active[object_name], axes, ref_len, ref_area, control),
                ],
                axis=1,
            )
        )
        names.append(f"{object_name}_candidate_count")
        names.extend(slot_names(object_name))

    for left, right in PAIR_FEATURES:
        left_keys = ["hand_top1", "hand_top2"] if left == "hand" else [left]
        right_keys = ["hand_top1", "hand_top2"] if right == "hand" else [right]
        valid = np.zeros(frames, dtype=np.float32)
        distance = np.full(frames, np.inf, dtype=np.float32)
        for left_key in left_keys:
            for right_key in right_keys:
                pair_valid = (active[left_key] & active[right_key]).astype(np.float32)
                pair_dist = np.linalg.norm(
                    features[left_key][:, 2:4] - features[right_key][:, 2:4], axis=1
                )
                pick = (pair_valid > 0) & (pair_dist < distance)
                valid[pick] = 1.0
                distance[pick] = pair_dist[pick]
        distance = np.where(
            valid > 0,
            np.clip(distance, 0.0, math.sqrt(2.0)) / math.sqrt(2.0),
            0.0,
        ).astype(np.float32)
        delta = np.zeros(frames, dtype=np.float32)
        if frames > 1:
            delta[1:] = np.clip(distance[1:] - distance[:-1], -1.0, 1.0)
            delta[valid <= 0] = 0.0
        blocks.append(np.stack([valid, distance, delta], axis=1).astype(np.float32))
        names.extend(
            [
                f"{left}_to_{right}_valid",
                f"{left}_to_{right}_dist",
                f"{left}_to_{right}_delta",
            ]
        )

    timeline = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    blocks.append(
        np.stack(
            [timeline, np.sin(2 * np.pi * timeline), np.cos(2 * np.pi * timeline)], axis=1
        )
    )
    names.extend(["t_norm", "t_sin", "t_cos"])
    matrix = _finite(np.concatenate(blocks, axis=1))
    if matrix.shape != (frames, FEATURE_DIM):
        raise AssertionError(
            f"clean_bbox_v3 base 矩阵形状异常: {tuple(matrix.shape)} != ({frames}, {FEATURE_DIM})"
        )
    return matrix, names


def build_clean_bbox_v3_features(
    frame_rows: list[list[tuple]],
    *,
    detection_mapping: dict[int, str],
    fps: float,
    confidence_default: float,
    mask_target_ids: frozenset[int] = frozenset(),
):
    """逐帧 bbox 路径构造 ``[T,113]``，返回矩阵、列名和实际 feature version。"""
    if not frame_rows:
        raise ValueError("CLEAN v3 特征至少需要一帧")
    if fps <= 0:
        raise ValueError("CLEAN v3 特征 fps 必须大于0")
    if not 0.0 <= confidence_default <= 1.0:
        raise ValueError("detection_confidence_default 必须在0..1")
    arrays = _read_object_arrays(
        frame_rows, detection_mapping, confidence_default, mask_target_ids
    )
    features, names = _build_base_matrix(arrays, len(frame_rows), fps)
    return features, names, VERSION


def clean_v3_feature_names() -> list[str]:
    """返回稳定列名，用于加载前校验 checkpoint 的 feature_names。"""
    arrays = {name: [] for name in OBJECTS}
    _features, names = _build_base_matrix(arrays, 1, 7.5)
    return names