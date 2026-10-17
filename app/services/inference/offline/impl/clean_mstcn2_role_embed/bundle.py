"""bundle 特征（94 列，ama-v5-bundle-94d）：各类检测框的位置、面积、速度，器械轴坐标，手与器械的相对位置。

    bundle = compute_bundle(frame_rows)      # bundle.columns：{列名: [T]}；bundle.features / active / frame 为中间量
    cols = bundle_columns(frame_rows)

裁剪自训练仓 ``tools/features_ama/``（extractor git_head 642d74f，经交付包 c7ref），只保留 bundle 用到的路径；
训练仓分 ``full`` / ``fix-compact-pair`` 两遍算再拼，这里一遍算完，数值逐位一致。函数名沿用训练仓，出处：

    § v2 绝对坐标块   clean_bbox_v2.py   读检测行、选槽位、短缺口插值、_build_base_matrix
    § v3 槽位投影     clean_bbox_v3.py   _slot_channels
    § v5 器械轴与拼装 clean_bbox_v5.py   scope_frame_offline、compact_keep、_pair_block（COMPACT_PAIRS 即 v4.V4_PAIRS）、build_bundle

硬约束：仅限离线（轴锚点双向填充、中值平滑用到未来帧）；废弃类在读入层剔除；留哪些列由 ``compact_keep`` 决定，
规则或计算方式的任何变动都要升特征版本并重训。
"""

from __future__ import annotations

import math
from typing import NamedTuple

import numpy as np

from .frames import CLASS_NAMES, FPS

CONFIDENCE_DEFAULT = 1.0  # 框文件不带置信度列，训练时按 1.0 计


# ==================== § v2 绝对坐标块（训练仓 clean_bbox_v2.py） ====================

OBJECTS = [
    "hand",
    "short_brush",
    "long_brush",
    "syringe",
    "air_gun",
    "scope_control_body",
    "scope_mid_section",
    "scope_distal_end",
    "brush_tip_out",
]

PAIR_FEATURES = [
    ("hand", "short_brush"),
    ("hand", "long_brush"),
    ("brush_tip_out", "scope_distal_end"),
    ("short_brush", "scope_control_body"),
    ("long_brush", "scope_mid_section"),
    ("air_gun", "scope_distal_end"),
    ("syringe", "scope_distal_end"),
]


def _finite(values: np.ndarray) -> np.ndarray:
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def _as_box5(row: np.ndarray) -> np.ndarray:
    if row.shape[0] >= 5:
        return row[:5].astype(np.float32)
    output = np.zeros(5, dtype=np.float32)
    output[: min(4, row.shape[0])] = row[:4]
    output[4] = 1.0 if output[0] > 0 else 0.0
    return output


def _box_score(row: np.ndarray, previous_center: np.ndarray | None = None) -> float:
    present, cx, cy, area, confidence = [float(value) for value in _as_box5(row)]
    if present <= 0:
        return -1.0
    score = confidence * math.sqrt(max(area, 1e-6))
    if previous_center is not None:
        score -= 0.15 * min(
            math.dist((cx, cy), tuple(previous_center)), math.sqrt(2.0)
        )
    return score


def _missing_age(raw_present: np.ndarray, max_gap: int) -> np.ndarray:
    output = np.zeros(len(raw_present), dtype=np.float32)
    age = 0
    for index, present in enumerate(raw_present > 0):
        age = 0 if present else age + 1
        output[index] = min(age, max_gap) / max(1, max_gap)
    return output


def _impute_short_gaps(raw: np.ndarray, fps: float, max_gap: int = 6):
    """短缺失段插值并生成 ``present/conf/cx/cy/area/speed/missing_age/imputed``。"""

    time_len = raw.shape[0]
    present = raw[:, 0].astype(np.float32)
    confidence = raw[:, 4].astype(np.float32)
    cx = raw[:, 1].astype(np.float32).copy()
    cy = raw[:, 2].astype(np.float32).copy()
    area = raw[:, 3].astype(np.float32).copy()
    imputed = np.zeros(time_len, dtype=np.float32)

    detected = np.where(present > 0)[0]
    if len(detected):
        for left, right in zip(detected[:-1], detected[1:]):
            gap = int(right - left - 1)
            if 0 < gap <= max_gap:
                for offset, index in enumerate(range(left + 1, right), start=1):
                    ratio = offset / (gap + 1)
                    cx[index] = (1 - ratio) * cx[left] + ratio * cx[right]
                    cy[index] = (1 - ratio) * cy[left] + ratio * cy[right]
                    area[index] = (1 - ratio) * area[left] + ratio * area[right]
                    confidence[index] = 0.5 * (
                        (1 - ratio) * confidence[left] + ratio * confidence[right]
                    )
                    imputed[index] = 1.0
        last = int(detected[-1])
        tail_gap = min(max_gap, time_len - last - 1)
        for index in range(last + 1, last + tail_gap + 1):
            cx[index], cy[index], area[index] = cx[last], cy[last], area[last]
            confidence[index] = 0.5 * confidence[last]
            imputed[index] = 1.0

    active = (present > 0) | (imputed > 0)
    centers = np.stack([cx, cy], axis=1)
    speed = np.zeros(time_len, dtype=np.float32)
    if time_len > 1:
        speed[1:] = np.clip(
            np.linalg.norm(np.diff(centers, axis=0), axis=1) * fps, 0.0, 5.0
        ) / 5.0
        speed[~active] = 0.0

    feature = np.stack(
        [present, confidence, cx, cy, area, speed, _missing_age(present, max_gap), imputed],
        axis=1,
    ).astype(np.float32)
    feature[~active, 1:6] = 0.0
    return feature, active


class _FrameDets:
    """一个目标类的全部检测，按帧分桶：``by_frame[t]`` = 该帧的检测行（float32 [5]），帧内按文件行序。

    发布包改动（与仓库版的唯一差别）：仓库版给每条检测分配一个 [T, 5] 稠密数组，选槽位时每帧遍历全部数组，
    耗时与内存随帧数平方增长。这里按帧分桶，候选顺序、打分与稳定排序不变，输出逐位一致。
    """

    __slots__ = ("by_frame",)

    def __init__(self):
        self.by_frame: dict[int, list[np.ndarray]] = {}

    def append(self, frame_index: int, row: np.ndarray) -> None:
        self.by_frame.setdefault(frame_index, []).append(row)

    def candidates(self, time_index: int) -> list[np.ndarray]:
        rows = self.by_frame.get(time_index, ())
        return [_as_box5(row) for row in rows if _as_box5(row)[0] > 0]


def _select_hand_slots(arrays: _FrameDets, frames: int):
    count = np.zeros(frames, dtype=np.float32)
    slots = [np.zeros((frames, 5), dtype=np.float32), np.zeros((frames, 5), dtype=np.float32)]
    for time_index in range(frames):
        candidates = arrays.candidates(time_index)
        count[time_index] = len(candidates)
        candidates.sort(key=_box_score, reverse=True)
        for slot_index, row in enumerate(candidates[:2]):
            slots[slot_index][time_index] = row
    return count, slots


def _select_top1_slot(arrays: _FrameDets, frames: int):
    count = np.zeros(frames, dtype=np.float32)
    slot = np.zeros((frames, 5), dtype=np.float32)
    previous_center = None
    for time_index in range(frames):
        candidates = arrays.candidates(time_index)
        count[time_index] = len(candidates)
        if not candidates:
            continue
        candidates.sort(key=lambda row: _box_score(row, previous_center), reverse=True)
        slot[time_index] = candidates[0]
        previous_center = slot[time_index, 1:3]
    return count, slot


def _build_base_matrix(object_arrays: dict[str, _FrameDets], frames: int, fps: float):
    blocks: list[np.ndarray] = []
    names: list[str] = []
    centers: dict[str, np.ndarray] = {}
    active: dict[str, np.ndarray] = {}

    hand_count, hand_slots = _select_hand_slots(object_arrays["hand"], frames)
    blocks.append((np.clip(hand_count, 0, 3) / 3.0)[:, None].astype(np.float32))
    names.append("hand_count")
    hand_centers, hand_active = [], []
    for slot_index, slot in enumerate(hand_slots, start=1):
        feature, slot_active = _impute_short_gaps(slot, fps)
        blocks.append(feature)
        names.extend(
            [
                f"hand_top{slot_index}_present",
                f"hand_top{slot_index}_conf",
                f"hand_top{slot_index}_cx",
                f"hand_top{slot_index}_cy",
                f"hand_top{slot_index}_area",
                f"hand_top{slot_index}_speed",
                f"hand_top{slot_index}_missing_age",
                f"hand_top{slot_index}_imputed",
            ]
        )
        hand_centers.append(feature[:, 2:4])
        hand_active.append(slot_active)
    centers["hand"] = np.stack(hand_centers, axis=0)
    active["hand"] = np.logical_or.reduce(hand_active)

    for object_name in OBJECTS:
        if object_name == "hand":
            continue
        count, slot = _select_top1_slot(object_arrays[object_name], frames)
        feature, object_active = _impute_short_gaps(slot, fps)
        blocks.append(
            np.concatenate([(np.clip(count, 0, 3) / 3.0)[:, None], feature], axis=1)
        )
        names.extend(
            [
                f"{object_name}_candidate_count",
                f"{object_name}_present",
                f"{object_name}_conf",
                f"{object_name}_cx",
                f"{object_name}_cy",
                f"{object_name}_area",
                f"{object_name}_speed",
                f"{object_name}_missing_age",
                f"{object_name}_imputed",
            ]
        )
        centers[object_name] = feature[:, 2:4]
        active[object_name] = object_active

    for left, right in PAIR_FEATURES:
        valid = (active[left] & active[right]).astype(np.float32)
        if left == "hand":
            distance = np.minimum(
                np.linalg.norm(centers["hand"][0] - centers[right], axis=1),
                np.linalg.norm(centers["hand"][1] - centers[right], axis=1),
            )
        elif right == "hand":
            distance = np.minimum(
                np.linalg.norm(centers[left] - centers["hand"][0], axis=1),
                np.linalg.norm(centers[left] - centers["hand"][1], axis=1),
            )
        else:
            distance = np.linalg.norm(centers[left] - centers[right], axis=1)
        distance = np.where(
            valid > 0,
            np.clip(distance, 0.0, math.sqrt(2.0)) / math.sqrt(2.0),
            0.0,
        ).astype(np.float32)
        delta = np.zeros(frames, dtype=np.float32)
        if frames > 1:
            delta[1:] = np.clip(distance[1:] - distance[:-1], -1.0, 1.0)
            delta[valid <= 0] = 0.0
        blocks.append(np.stack([valid, distance, delta], axis=1))
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
    return _finite(np.concatenate(blocks, axis=1)), names


def _empty_object_arrays(frames: int) -> dict[str, _FrameDets]:
    return {name: _FrameDets() for name in OBJECTS}


def _read_object_arrays(
    frame_rows: list[list[tuple]],
    detection_mapping: dict[int, str],
    confidence_default: float,
    mask_target_ids: frozenset[int],
):
    """逐帧检测行 ``(class, cx, cy, w, h[, confidence])`` → 每个目标类一个按帧分桶的检测集合（``_FrameDets``）。

    后端改动：仓库版逐帧读 YOLO 文本文件，这里直接收已解析的行，其余口径不变。
    """

    frame_count = len(frame_rows)
    arrays = _empty_object_arrays(frame_count)
    for frame_index, rows in enumerate(frame_rows):
        for parts in rows:
            if len(parts) not in {5, 6}:
                continue
            class_id = int(parts[0])
            if class_id in mask_target_ids:
                continue
            object_name = detection_mapping.get(class_id)
            if object_name not in arrays:
                continue
            cx, cy, width, height = (float(value) for value in parts[1:5])
            confidence = float(parts[5]) if len(parts) == 6 else confidence_default
            row = np.array(
                (
                    1.0,
                    min(max(cx, 0.0), 1.0),
                    min(max(cy, 0.0), 1.0),
                    min(max(width, 0.0), 1.0) * min(max(height, 0.0), 1.0),
                    min(max(confidence, 0.0), 1.0),
                ),
                dtype=np.float32,
            )
            arrays[object_name].append(frame_index, row)
    return arrays


# ==================== § v3 槽位投影（训练仓 clean_bbox_v3.py） ====================

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


# ==================== § v5 器械轴与拼装（训练仓 clean_bbox_v5.py） ====================

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
# 训练仓 clean_bbox_v4.V4_PAIRS
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
    _count, hand_slots = _select_hand_slots(arrays["hand"], frames)
    for slot_index, slot in enumerate(hand_slots, start=1):
        key = f"hand_top{slot_index}"
        features[key], active[key] = _impute_short_gaps(slot, fps)
    for name in OBJECTS:
        if name == "hand":
            continue
        _count, slot = _select_top1_slot(arrays[name], frames)
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


# ==================== 入口 ====================


def compute_bundle(frame_rows: list[list[tuple]]) -> Bundle:
    return build_bundle(frame_rows, detection_mapping=CLASS_NAMES, fps=FPS, confidence_default=CONFIDENCE_DEFAULT)


def bundle_columns(frame_rows: list[list[tuple]]) -> dict[str, np.ndarray]:
    """单视频 bundle 94 列：{列名: [T]}（float32）。"""
    return compute_bundle(frame_rows).columns
