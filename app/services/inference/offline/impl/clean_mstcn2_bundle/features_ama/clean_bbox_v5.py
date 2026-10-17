"""bundle 94 列（ama-v5-bundle-94d）的单遍计算：v2 绝对坐标块 ⊕ ctrl→mid 器械轴几何通道 ⊕ 轴观测 ⊕ hand / 锚点 pair。

    result = build_bundle(frame_rows, detection_mapping=..., fps=15.0, confidence_default=1.0)
    result.columns            # {列名: [T] float32}，94 列；result.features / active / frame 供调用方复用

硬约束：
- 仅限离线：轴锚点双向填充、中值平滑都会用到未来帧，不能用于流式推理；
- 废弃类（scope_distal_end / short_brush / long_brush）在读入层剔除；留哪些列由列名规则 ``compact_keep`` 决定，规则变动必须升版本。
- 训练仓的同名实现分 ``full`` 与 ``fix-compact-pair`` 两个 recipe 各算一遍再拼；这里一次算完，逐列取自同一组函数调用，数值逐位一致。
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np

from .clean_bbox_v2 import (
    OBJECTS,
    PAIR_FEATURES,
    _build_base_matrix,
    _impute_short_gaps,
    _read_object_arrays,
    _select_hand_slots,
    _select_top1_slot,
)
from .clean_bbox_v3 import (
    _FALLBACK_REF_AREA,
    _FALLBACK_REF_LEN,
    V3_SLOT_CHANNELS,
    _slot_channels,
)

DEPRECATED = frozenset({"scope_distal_end", "short_brush", "long_brush"})
BUNDLE_DIM = 94

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
# 原 clean_bbox_v4.V4_PAIRS（v4 其余部分 bundle 不用，已删）
COMPACT_PAIRS = (("hand", "scope_control_body"), ("hand", "scope_mid_section"), ("scope_control_body", "scope_mid_section"))

MIN_AXIS_LEN = 0.03
SMOOTH_SECONDS = 1.0
AGE_CAP_SECONDS = 5.0


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


class Bundle(NamedTuple):
    columns: dict  # {列名: [T] float32}，94 列
    features: dict  # 各槽位插值后的 [T,8]（hand_top1 / hand_top2 / 各目标）
    active: dict  # 各槽位 [T] bool
    frame: ScopeFrame  # 平滑后的 ctrl→mid 器械轴


def build_bundle(
    frame_rows: list[list[tuple]],
    *,
    detection_mapping: dict[int, str],
    fps: float,
    confidence_default: float,
) -> Bundle:
    """逐视频 bundle 94 列 + 中间量。列名、取值与训练仓 ``build_candidate_columns``（full + fix-compact-pair）逐位一致。"""
    if not frame_rows:
        raise ValueError("bundle 特征至少需要一帧")
    if fps <= 0:
        raise ValueError("bundle 特征 fps 必须大于0")
    if not 0.0 <= confidence_default <= 1.0:
        raise ValueError("detection_confidence_default 必须在0..1")
    filtered = {cid: name for cid, name in detection_mapping.items() if name not in DEPRECATED}
    arrays = _read_object_arrays(frame_rows, filtered, confidence_default, frozenset())
    frames = len(frame_rows)
    columns: dict[str, np.ndarray] = {}

    def put(name: str, values: np.ndarray) -> None:
        columns[name] = np.nan_to_num(np.asarray(values, dtype=np.float32))

    v2_feats, v2_names = _build_base_matrix(arrays, frames, fps)
    for index, name in enumerate(v2_names):
        if compact_keep(f"abs/{name}"):
            put(f"abs/{name}", v2_feats[:, index])

    features, active = _object_tracks(arrays, frames, fps)
    frame = scope_frame_offline(features, active, frames, fps, smooth=True)
    for key in ["hand_top1", "hand_top2"] + [name for name in OBJECTS if name != "hand"]:
        kept = [(i, f"scope/{key}_{ch}") for i, ch in enumerate(V3_SLOT_CHANNELS) if compact_keep(f"scope/{key}_{ch}")]
        if not kept:
            continue
        channels = _slot_channels(features[key], active[key], frame.axes, frame.ref_len, frame.ref_area, frame.origin)
        for index, name in kept:
            put(name, channels[:, index])

    put("scope/axis_observed", frame.observed.astype(np.float32))
    put("scope/axis_age", np.minimum(frame.age_seconds, AGE_CAP_SECONDS) / AGE_CAP_SECONDS)
    pairs, pair_names = _pair_block(features, active, frames)
    for index, name in enumerate(pair_names):
        put(name, pairs[:, index])
    if len(columns) != BUNDLE_DIM:
        raise AssertionError(f"bundle 列数 {len(columns)} != {BUNDLE_DIM}")
    return Bundle(columns, features, active, frame)
