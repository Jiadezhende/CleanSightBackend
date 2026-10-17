"""CLEAN clean_bbox_v4 特征（60 维，2026-09-19 设计 docs/FEATURE_SCHEME_V4_DESIGN.md）。

v4 三项变更（相对 v3）：
- **废弃目标剔除**：scope_distal_end（presence 8~12%）与 short_brush（0.4~3%）
  检测不可靠，全部通道剔除；核心类集合 8 → 6。
- **事件线索类保留但不作定位判据**：syringe / air_gun / brush_tip_out 只保留
  presence + 被 scope 轴定位的坐标 + 面积比（4 通道，无速度/计数）；禁止作轴
  锚点、参考基准或 pair 基准端（由本模块结构保证：轴与基准只用 ctrl/mid）。
- **主锚点转正**：器械轴主锚点 v3 的 ctrl→distal（可用率仅 8%）改为
  ctrl→mid（同帧共现 63~69%）；面积参考基准 mid 面积（缺失回退 ctrl）。

块布局（60 维 = 1+16+7+9+15+9+3）：hand_count(1) + hand×2slot×8(16) +
ctrl(count+6 通道，轴端点不编码 along/across) + mid(count+8 通道) +
事件类×3×(count+4 通道)(15) + pair×3×3(9: hand-ctrl/hand-mid/ctrl-mid) + 时间(3)。
（设计文档原写 62 为扣减笔误，按命名规范以实现维度 60 入契约名。）
槽位选择与短缺失插值复用 clean_bbox_v2 实现；连续列 clip 有界，不依赖全局 z-score。
"""

from __future__ import annotations

import math

import numpy as np

from .clean_bbox_v3 import _forward_fill
from .clean_bbox_v2 import (
    _finite,
    _impute_short_gaps,
    _read_object_arrays,
    _select_hand_slots,
    _select_top1_slot,
)

VERSION = "ama-v4-scope-6c-60d"
CLEAN_V4_VERSION = VERSION
FEATURE_DIM = 60
CLEAN_V4_FEATURE_DIMS = {VERSION: FEATURE_DIM}

CLIP_POS = 4.0
CLIP_LOG = 6.0
FALLBACK_REF_LEN = 0.35
FALLBACK_REF_AREA = 0.02

CORE_OBJECTS = ["hand", "scope_control_body", "scope_mid_section"]
EVENT_OBJECTS = ["syringe", "air_gun", "brush_tip_out"]
V4_OBJECTS = CORE_OBJECTS + EVENT_OBJECTS
V4_PAIRS = [("hand", "scope_control_body"), ("hand", "scope_mid_section"),
            ("scope_control_body", "scope_mid_section")]

SLOT_CHANNELS = [
    "present", "conf", "along", "across", "log_area_ratio", "speed", "missing_age", "imputed",
]
EVENT_CHANNELS = ["present", "along", "across", "log_area_ratio"]

def _v4_frame(features: dict, active: dict, frames: int):
    """v4 器械轴：主锚点 ctrl→mid（转正），回退前帧轴；基准面积 mid（回退 ctrl）。"""
    control = features["scope_control_body"][:, 2:4]
    mid = features["scope_mid_section"][:, 2:4]
    active_ctl = active["scope_control_body"]
    active_mid = active["scope_mid_section"]

    axes = np.zeros((frames, 2), dtype=np.float32)
    ref_len = np.full(frames, FALLBACK_REF_LEN, dtype=np.float32)
    last_axis = None
    valid_lens = []
    len_valid = np.zeros(frames, dtype=bool)
    for index in range(frames):
        if active_ctl[index] and active_mid[index]:
            vector = mid[index] - control[index]
            length = float(np.linalg.norm(vector))
            if length > 1e-4:
                last_axis = (vector / length).astype(np.float32)
                ref_len[index] = length
                len_valid[index] = True
                valid_lens.append(length)
        axes[index] = last_axis if last_axis is not None else np.array([1.0, 0.0], np.float32)
    if valid_lens:
        median_len = float(np.median(valid_lens))
        ref_len = np.where(
            len_valid, ref_len, _forward_fill(ref_len, len_valid, median_len)
        ).astype(np.float32)

    # 面积参考基准：mid 面积（缺失帧回退 ctrl 面积，再回退常数）
    mid_area = features["scope_mid_section"][:, 4]
    ctl_area = features["scope_control_body"][:, 4]
    base = np.where(active_mid, mid_area, ctl_area)
    base_valid = active_mid | active_ctl
    if base_valid.any():
        median_area = float(np.median(base[base_valid]))
        ref_area = np.where(
            base_valid, base,
            _forward_fill(np.where(base_valid, base, median_area), base_valid, median_area),
        ).astype(np.float32)
    else:
        ref_area = np.full(frames, FALLBACK_REF_AREA, dtype=np.float32)
    return axes, ref_len, ref_area

def _slot_channels(feature, active, axes, ref_len, ref_area, control):
    """核心类 8 通道（同 v3 口径：present/conf/along/across/log_area_ratio/speed/missing_age/imputed）。"""
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
    channels = np.stack([
        present, confidence,
        np.clip(along, -CLIP_POS, CLIP_POS),
        np.clip(across, -CLIP_POS, CLIP_POS),
        np.clip(log_ratio, -CLIP_LOG, CLIP_LOG),
        speed, missing_age, imputed,
    ], axis=1).astype(np.float32)
    channels[~active, 1:6] = 0.0
    return channels

def _event_channels(feature, active, axes, ref_len, ref_area, control):
    """事件线索类 4 通道：presence + 被轴定位坐标 + 面积比（无速度/置信度/缺失龄——事件帧过少）。"""
    present = feature[:, 0]
    centers = feature[:, 2:4]
    area = feature[:, 4]
    relative = centers - control
    along = np.einsum("td,td->t", relative, axes) / ref_len
    perpendicular = np.stack([-axes[:, 1], axes[:, 0]], axis=1)
    across = np.einsum("td,td->t", relative, perpendicular) / ref_len
    log_ratio = np.log((area + 1e-4) / (ref_area + 1e-4))
    channels = np.stack([
        present,
        np.clip(along, -CLIP_POS, CLIP_POS),
        np.clip(across, -CLIP_POS, CLIP_POS),
        np.clip(log_ratio, -CLIP_LOG, CLIP_LOG),
    ], axis=1).astype(np.float32)
    channels[~active, 1:4] = 0.0
    return channels

def _build_base_matrix(object_arrays: dict, frames: int, fps: float):
    features = {}
    active = {}
    blocks = []
    names = []

    hand_count, hand_slots = _select_hand_slots(object_arrays.get("hand", []), frames)
    blocks.append((np.clip(hand_count, 0, 3) / 3.0)[:, None].astype(np.float32))
    names.append("hand_count")
    for slot_index, slot in enumerate(hand_slots, start=1):
        feature, slot_active = _impute_short_gaps(slot, fps)
        features[f"hand_top{slot_index}"] = feature
        active[f"hand_top{slot_index}"] = slot_active
    for object_name in V4_OBJECTS:
        if object_name == "hand":
            continue
        _count, slot = _select_top1_slot(object_arrays.get(object_name, []), frames)
        feature, object_active = _impute_short_gaps(slot, fps)
        features[object_name] = feature
        active[object_name] = object_active

    axes, ref_len, ref_area = _v4_frame(features, active, frames)
    control = features["scope_control_body"][:, 2:4]

    for slot_index in (1, 2):
        key = f"hand_top{slot_index}"
        blocks.append(_slot_channels(features[key], active[key], axes, ref_len, ref_area, control))
        names.extend([f"hand_top{slot_index}_{c}" for c in SLOT_CHANNELS])

    # ctrl：轴定义端点（along/across 恒 0 不编码）——count + 6 通道
    count, _slot = _select_top1_slot(object_arrays.get("scope_control_body", []), frames)
    ctl_block = np.concatenate(
        [
            (np.clip(count, 0, 3) / 3.0)[:, None].astype(np.float32),
            _slot_channels(features["scope_control_body"], active["scope_control_body"],
                           axes, ref_len, ref_area, control),
        ], axis=1)
    # 拼接后列序：[count, present, conf, along, across, log, speed, missing, imputed]
    # 保留 count/present/conf/log/speed/missing/imputed，砍 along/across（端点恒 0）
    keep = [0, 1, 2, 5, 6, 7, 8]
    blocks.append(ctl_block[:, keep])
    names.extend(["scope_control_body_candidate_count"] +
                 [f"scope_control_body_{SLOT_CHANNELS[i - 1]}" for i in keep[1:]])

    # mid：count + 8 通道（轴另一端，编码完整——along≈轴长归一值本身有信息）
    count, _slot = _select_top1_slot(object_arrays.get("scope_mid_section", []), frames)
    blocks.append(np.concatenate(
        [
            (np.clip(count, 0, 3) / 3.0)[:, None].astype(np.float32),
            _slot_channels(features["scope_mid_section"], active["scope_mid_section"],
                           axes, ref_len, ref_area, control),
        ], axis=1))
    names.extend(["scope_mid_section_candidate_count"] +
                 [f"scope_mid_section_{c}" for c in SLOT_CHANNELS])

    # 事件类：count + 4 通道（count 保留——事件出现帧数本身是判别信号）
    for object_name in EVENT_OBJECTS:
        count, _slot = _select_top1_slot(object_arrays.get(object_name, []), frames)
        blocks.append(np.concatenate(
            [
                (np.clip(count, 0, 3) / 3.0)[:, None].astype(np.float32),
                _event_channels(features[object_name], active[object_name],
                                axes, ref_len, ref_area, control),
            ], axis=1))
        names.extend([f"{object_name}_candidate_count"] +
                     [f"{object_name}_{c}" for c in EVENT_CHANNELS])

    for left, right in V4_PAIRS:
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
            valid > 0, np.clip(distance, 0.0, math.sqrt(2.0)) / math.sqrt(2.0), 0.0,
        ).astype(np.float32)
        delta = np.zeros(frames, dtype=np.float32)
        if frames > 1:
            delta[1:] = np.clip(distance[1:] - distance[:-1], -1.0, 1.0)
            delta[valid <= 0] = 0.0
        blocks.append(np.stack([valid, distance, delta], axis=1).astype(np.float32))
        names.extend([f"{left}_to_{right}_valid", f"{left}_to_{right}_dist",
                      f"{left}_to_{right}_delta"])

    timeline = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    blocks.append(np.stack(
        [timeline, np.sin(2 * math.pi * timeline), np.cos(2 * math.pi * timeline)], axis=1
    ))
    names.extend(["t_norm", "t_sin", "t_cos"])
    matrix = _finite(np.concatenate(blocks, axis=1))
    if matrix.shape != (frames, FEATURE_DIM):
        raise AssertionError(
            f"clean_bbox_v4 矩阵形状异常: {tuple(matrix.shape)} != ({frames}, {FEATURE_DIM})"
        )
    return matrix, names

def build_clean_bbox_v4_features(
    frame_rows: list[list[tuple]],
    *,
    detection_mapping: dict[int, str],
    fps: float,
    confidence_default: float,
    mask_target_ids: frozenset[int] = frozenset(),
):
    """逐帧 bbox 路径构造 [T,60]。detection_mapping 仍为 8 类表，废弃类在读入层丢弃。"""
    if not frame_rows:
        raise ValueError("CLEAN v4 特征至少需要一帧")
    if fps <= 0:
        raise ValueError("CLEAN v4 特征 fps 必须大于0")
    if not 0.0 <= confidence_default <= 1.0:
        raise ValueError("detection_confidence_default 必须在0..1")
    deprecated = {"scope_distal_end", "short_brush"}
    mapped = {i: n for i, n in detection_mapping.items() if n not in deprecated}
    arrays = _read_object_arrays(frame_rows, mapped, confidence_default, mask_target_ids)
    features, names = _build_base_matrix(arrays, len(frame_rows), fps)
    return features, names, VERSION

def clean_v4_feature_names() -> list[str]:
    arrays = {name: [] for name in V4_OBJECTS}
    _features, names = _build_base_matrix(arrays, 1, 7.5)
    return names