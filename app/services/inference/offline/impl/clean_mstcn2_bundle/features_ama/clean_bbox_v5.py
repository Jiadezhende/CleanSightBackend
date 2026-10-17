"""ama-v5 离线特征：v2 绝对坐标块 ⊕ v3 器械轴块（轴修复版）⊕ 按 recipe 追加的通道。

用法::

    feats, names, version = build_ama_v5_features(
        frame_rows, detection_mapping=..., fps=..., confidence_default=..., recipe="fix")

硬约束：
- 仅限离线：轴锚点双向填充、中值平滑、中心差分速度都会用到未来帧，不能用于流式推理；
- 输入必须包含全部检测类的行，废弃类（scope_distal_end / short_brush / long_brush）由本模块剔除；
- recipe 就是契约（维度见 ``RECIPES``），换 recipe 必须重训；
- fix-compact 系列按列名规则 ``compact_keep`` 从 fix-226 选列，规则或列序变动必须升版本；
- 放在 framework 的 ``temporal/features/`` 下，与 clean_bbox_v2 / v3 / v4 同包。
设计与实验方案见 MODEL_SCHEME_V5.md。
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

from .clean_bbox_v2 import (
    OBJECTS,
    PAIR_FEATURES,
    _impute_short_gaps,
    _read_object_arrays,
    _select_hand_slots,
    _select_top1_slot,
    build_clean_bbox_features,
)
from .clean_bbox_v3 import (
    _CLIP_POS,
    _FALLBACK_REF_AREA,
    _FALLBACK_REF_LEN,
    V3_SLOT_CHANNELS,
    _slot_channels,
)
from .clean_bbox_v3 import _build_base_matrix as _build_v3_matrix
from .clean_bbox_v4 import V4_PAIRS

DEPRECATED = frozenset({"scope_distal_end", "short_brush", "long_brush"})
RECIPES = {"fix": 226, "smooth": 228, "full": 234, "fix-compact": 83, "fix-compact-pair": 92}
VERSIONS = {recipe: f"ama-v5-{recipe}-{dim}d" for recipe, dim in RECIPES.items()}
AMA_V5_FEATURE_DIMS = {VERSIONS[recipe]: dim for recipe, dim in RECIPES.items()}
AMA_V5_RECIPE_BY_VERSION = {version: recipe for recipe, version in VERSIONS.items()}
SMOOTH_RECIPES = frozenset({"smooth", "full"})
COMPACT_RECIPES = frozenset({"fix-compact", "fix-compact-pair"})

# 紧凑契约的列名规则（作用于 fix-226 的列名）：
# CP1 废弃类槽位列、任一端为废弃类的 pair 列（两块都删）；
DEAD_PAIR_COLUMNS = frozenset(
    f"{left}_to_{right}_{suffix}"
    for left, right in PAIR_FEATURES
    if left in DEPRECATED or right in DEPRECATED
    for suffix in ("valid", "dist", "delta")
)
# CP2 scope 块只留几何通道，其余（状态通道/计数/时间）与 abs 块逐元素相同；
COMPACT_SCOPE_CHANNELS = ("along", "across", "log_area_ratio")
# CP3 轴退化列：原点即 ctrl → ctrl 的 along/across ≡ 0；面积基准即 mid → mid 的 log_area_ratio ≡ 0。
AXIS_DEGENERATE = frozenset(
    {
        "scope/scope_control_body_along",
        "scope/scope_control_body_across",
        "scope/scope_mid_section_log_area_ratio",
    }
)
COMPACT_PAIRS = tuple(V4_PAIRS)

MIN_AXIS_LEN = 0.03
SMOOTH_SECONDS = 1.0
AGE_CAP_SECONDS = 5.0
VEL_SPANS_SECONDS = (0.5, 1.0, 2.0)
VEL_CLIP = 2.0
VEL_OBJECTS = ("hand_near", "brush_tip_out")


class ScopeFrame(NamedTuple):
    origin: np.ndarray  # [T,2]
    axes: np.ndarray  # [T,2] 单位向量，ctrl→mid
    ref_len: np.ndarray  # [T]
    ref_area: np.ndarray  # [T]
    observed: np.ndarray  # [T] bool，当帧两个锚点都有效且间距可信
    age_seconds: np.ndarray  # [T] 距最近一次锚点有效帧的秒数


def _fill(values: np.ndarray, valid: np.ndarray):
    """有效帧之间线性插值，两端用常值外推；返回 (filled [T,d], age_frames [T])，无有效帧返回 (None, None)。"""
    index = np.flatnonzero(valid)
    if index.size == 0:
        return None, None
    time = np.arange(len(valid))
    values = values.reshape(len(valid), -1)
    filled = np.stack(
        [np.interp(time, index, values[index, col]) for col in range(values.shape[1])], axis=1
    )
    position = np.searchsorted(index, time)
    left = index[np.clip(position - 1, 0, index.size - 1)]
    right = index[np.clip(position, 0, index.size - 1)]
    age = np.minimum(np.abs(time - left), np.abs(right - time))
    return filled.astype(np.float32), age.astype(np.float32)


def _median_smooth(values: np.ndarray, window: int) -> np.ndarray:
    if window <= 1 or len(values) < 2:
        return values
    half = window // 2
    padded = np.pad(values, ((half, half), (0, 0)), mode="edge")
    view = np.lib.stride_tricks.sliding_window_view(padded, window, axis=0)
    return np.median(view, axis=-1).astype(np.float32)


def _object_tracks(arrays: dict, frames: int, fps: float):
    """与 v3 相同的槽位选择 + 短缺失插值，返回 (features[key] [T,8], active[key] [T])。"""
    features: dict[str, np.ndarray] = {}
    active: dict[str, np.ndarray] = {}
    _count, hand_slots = _select_hand_slots(arrays.get("hand", []), frames)
    for slot_index, slot in enumerate(hand_slots, start=1):
        key = f"hand_top{slot_index}"
        features[key], active[key] = _impute_short_gaps(slot, fps)
    for name in OBJECTS:
        if name == "hand":
            continue
        _count, slot = _select_top1_slot(arrays.get(name, []), frames)
        features[name], active[name] = _impute_short_gaps(slot, fps)
    return features, active


def scope_frame_offline(
    features: dict, active: dict, frames: int, fps: float, *, smooth: bool
) -> ScopeFrame:
    """ctrl→mid 器械轴：锚点双向填充（可选中值平滑），原点与轴共用同一套回退。"""
    ctrl_ok = active["scope_control_body"]
    mid_ok = active["scope_mid_section"]
    ctrl, ctrl_age = _fill(features["scope_control_body"][:, 2:4], ctrl_ok)
    mid, mid_age = _fill(features["scope_mid_section"][:, 2:4], mid_ok)
    window = (max(1, int(round(fps * SMOOTH_SECONDS))) | 1) if smooth else 1
    if ctrl is not None:
        ctrl = _median_smooth(ctrl, window)
    if mid is not None:
        mid = _median_smooth(mid, window)

    if ctrl is not None:
        origin = ctrl
    elif mid is not None:
        origin = mid
    else:
        origin = np.full((frames, 2), 0.5, dtype=np.float32)

    axes = np.tile(np.array([1.0, 0.0], dtype=np.float32), (frames, 1))
    ref_len = np.full(frames, _FALLBACK_REF_LEN, dtype=np.float32)
    observed = np.zeros(frames, dtype=bool)
    age = np.full(frames, AGE_CAP_SECONDS, dtype=np.float32)
    if ctrl is not None and mid is not None:
        vector = mid - ctrl
        length = np.linalg.norm(vector, axis=1)
        good = length > MIN_AXIS_LEN
        unit, _ = _fill(vector / np.maximum(length, 1e-6)[:, None], good)
        if unit is not None:
            axes = unit / np.maximum(np.linalg.norm(unit, axis=1, keepdims=True), 1e-6)
            ref_len = _fill(length, good)[0][:, 0]
            observed = good & ctrl_ok & mid_ok
            age = np.maximum(ctrl_age, mid_age) / fps

    ref_area = np.full(frames, _FALLBACK_REF_AREA, dtype=np.float32)
    for name in ("scope_mid_section", "scope_control_body"):
        area, _ = _fill(features[name][:, 4], active[name])
        if area is not None:
            ref_area = area[:, 0]
            break

    return ScopeFrame(
        origin.astype(np.float32),
        axes.astype(np.float32),
        ref_len.astype(np.float32),
        ref_area.astype(np.float32),
        observed,
        age.astype(np.float32),
    )


def _scope_block(arrays: dict, frames: int, fps: float, *, smooth: bool):
    """v3 113 维布局原样保留，只用修复后的坐标系重算各槽位通道。"""
    matrix, names = _build_v3_matrix(arrays, frames, fps)
    features, active = _object_tracks(arrays, frames, fps)
    frame = scope_frame_offline(features, active, frames, fps, smooth=smooth)
    width = len(V3_SLOT_CHANNELS)
    for key in ["hand_top1", "hand_top2"] + [name for name in OBJECTS if name != "hand"]:
        start = names.index(f"{key}_{V3_SLOT_CHANNELS[0]}")
        matrix[:, start : start + width] = _slot_channels(
            features[key], active[key], frame.axes, frame.ref_len, frame.ref_area, frame.origin
        )
    return matrix, names, features, active, frame


def _near_hand(features: dict, active: dict, origin: np.ndarray):
    """逐帧取离器械轴原点更近的那只手（送刷的手通常靠近镜口）。"""
    centers = [features[f"hand_top{index}"][:, 2:4] for index in (1, 2)]
    oks = [active[f"hand_top{index}"] for index in (1, 2)]
    dists = [
        np.where(ok, np.linalg.norm(center - origin, axis=1), np.inf)
        for center, ok in zip(centers, oks)
    ]
    pick_second = dists[1] < dists[0]
    return np.where(pick_second[:, None], centers[1], centers[0]), oks[0] | oks[1]


def _velocity_block(features: dict, active: dict, frame: ScopeFrame, fps: float):
    """沿器械轴的有符号速度（中心差分，多时间尺度），单位为轴长/秒，归一化到 [-1,1]。"""
    tracks = {
        "hand_near": _near_hand(features, active, frame.origin),
        "brush_tip_out": (features["brush_tip_out"][:, 2:4], active["brush_tip_out"]),
    }
    frames = len(frame.origin)
    time = np.arange(frames)
    columns, names = [], []
    for name in VEL_OBJECTS:
        center, ok = tracks[name]
        along = np.einsum("td,td->t", center - frame.origin, frame.axes) / frame.ref_len
        along = np.clip(along, -_CLIP_POS, _CLIP_POS)
        for span in VEL_SPANS_SECONDS:
            half = max(1, int(round(span * fps / 2)))
            low = np.clip(time - half, 0, frames - 1)
            high = np.clip(time + half, 0, frames - 1)
            valid = ok[low] & ok[high] & (high > low)
            velocity = (along[high] - along[low]) * fps / np.maximum(high - low, 1)
            velocity = np.clip(velocity, -VEL_CLIP, VEL_CLIP) / VEL_CLIP
            velocity[~valid] = 0.0
            columns.append(velocity.astype(np.float32))
            names.append(f"{name}_v_along_{span:g}s")
    return np.stack(columns, axis=1), names


def compact_keep(name: str) -> bool:
    """fix-226 列名 → 是否进入紧凑契约（规则 R1–R3，见模块常量）。"""
    block, _, column = name.partition("/")
    if column in DEAD_PAIR_COLUMNS or any(column.startswith(f"{obj}_") for obj in DEPRECATED):
        return False
    if block == "abs":
        return True
    if name in AXIS_DEGENERATE:
        return False
    return column.endswith(tuple(f"_{channel}" for channel in COMPACT_SCOPE_CHANNELS))


def _pair_block(features: dict, active: dict, frames: int):
    """V4_PAIRS 的 valid/dist/delta，口径与 clean_bbox_v4 相同：图像坐标距离 / √2，hand 取有效槽位中更近的一只。"""
    columns, names = [], []
    for left, right in COMPACT_PAIRS:
        left_keys = ["hand_top1", "hand_top2"] if left == "hand" else [left]
        right_keys = ["hand_top1", "hand_top2"] if right == "hand" else [right]
        valid = np.zeros(frames, dtype=np.float32)
        distance = np.full(frames, np.inf, dtype=np.float32)
        for left_key in left_keys:
            for right_key in right_keys:
                pair_valid = active[left_key] & active[right_key]
                pair_dist = np.linalg.norm(
                    features[left_key][:, 2:4] - features[right_key][:, 2:4], axis=1
                )
                pick = pair_valid & (pair_dist < distance)
                valid[pick] = 1.0
                distance[pick] = pair_dist[pick]
        distance = np.where(
            valid > 0, np.clip(distance, 0.0, np.sqrt(2.0)) / np.sqrt(2.0), 0.0
        ).astype(np.float32)
        delta = np.zeros(frames, dtype=np.float32)
        if frames > 1:
            delta[1:] = np.clip(distance[1:] - distance[:-1], -1.0, 1.0)
            delta[valid <= 0] = 0.0
        columns += [valid, distance, delta]
        names += [f"pair/{left}_to_{right}_{suffix}" for suffix in ("valid", "dist", "delta")]
    return np.stack(columns, axis=1), names


def build_ama_v5_features(
    frame_rows: list[list[tuple]],
    *,
    detection_mapping: dict[int, str],
    fps: float,
    confidence_default: float,
    mask_target_ids: frozenset[int] = frozenset(),
    recipe: str = "fix",
):
    """逐视频 [T,D]；返回 (矩阵, 列名, 契约版本)。"""
    if recipe not in RECIPES:
        raise ValueError(f"未知 recipe={recipe!r}；可用: {sorted(RECIPES)}")
    filtered = {cid: name for cid, name in detection_mapping.items() if name not in DEPRECATED}
    v2_feats, v2_names, _ = build_clean_bbox_features(
        frame_rows,
        detection_mapping=filtered,
        feature_version="clean_bbox_v2_top1_impute",
        fps=fps,
        confidence_default=confidence_default,
        mask_target_ids=mask_target_ids,
    )
    arrays = _read_object_arrays(frame_rows, filtered, confidence_default, mask_target_ids)
    frames = len(frame_rows)
    v3_feats, v3_names, features, active, frame = _scope_block(
        arrays, frames, fps, smooth=recipe in SMOOTH_RECIPES
    )

    blocks = [v2_feats, v3_feats]
    names = [f"abs/{name}" for name in v2_names] + [f"scope/{name}" for name in v3_names]
    if recipe in COMPACT_RECIPES:
        keep = [index for index, name in enumerate(names) if compact_keep(name)]
        blocks = [np.concatenate(blocks, axis=1)[:, keep]]
        names = [names[index] for index in keep]
    if recipe == "fix-compact-pair":
        pairs, pair_names = _pair_block(features, active, frames)
        blocks.append(pairs)
        names += pair_names
    if recipe in SMOOTH_RECIPES:
        age = np.minimum(frame.age_seconds, AGE_CAP_SECONDS) / AGE_CAP_SECONDS
        blocks.append(np.stack([frame.observed.astype(np.float32), age], axis=1))
        names += ["scope/axis_observed", "scope/axis_age"]
    if recipe == "full":
        velocity, velocity_names = _velocity_block(features, active, frame, fps)
        blocks.append(velocity)
        names += [f"scope/{name}" for name in velocity_names]

    feats = np.nan_to_num(np.concatenate(blocks, axis=1)).astype(np.float32)
    if feats.shape != (frames, RECIPES[recipe]):
        raise AssertionError(f"{VERSIONS[recipe]} 形状异常: {feats.shape}")
    return feats, names, VERSIONS[recipe]


def ama_v5_feature_names(recipe: str) -> list[str]:
    """recipe 的稳定列名（不读数据），用于校验 checkpoint 的 feature_names。"""
    _feats, names, _version = build_ama_v5_features(
        [[]],
        detection_mapping={},
        fps=7.5,
        confidence_default=1.0,
        recipe=recipe,
    )
    return names
