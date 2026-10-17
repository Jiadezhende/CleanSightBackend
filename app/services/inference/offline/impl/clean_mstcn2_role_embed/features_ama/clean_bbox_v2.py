"""ActionMixed bbox 序列到 v2 基础 113 维特征（bundle 的 abs 块）。

数学口径迁自 CleanSightBackend 的 CLEAN offline segmenter。输入是当前模型仓库逐帧 YOLO
文本：``class cx cy w h [confidence]``。五列标注没有置信度时使用 YAML 明确声明的
``feature_schema.detection_confidence_default``；因此该路径只能做 exploratory 评测，不能
冒充真实 YOLO 检测置信度下的正式结果。
"""

from __future__ import annotations

import math

import numpy as np


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


def _frame_candidates(dets, time_index: int) -> list[np.ndarray]:
    return dets.candidates(time_index) if isinstance(dets, _FrameDets) else []


def _select_hand_slots(arrays: "_FrameDets | list", frames: int):
    count = np.zeros(frames, dtype=np.float32)
    slots = [np.zeros((frames, 5), dtype=np.float32), np.zeros((frames, 5), dtype=np.float32)]
    for time_index in range(frames):
        candidates = _frame_candidates(arrays, time_index)
        count[time_index] = len(candidates)
        candidates.sort(key=_box_score, reverse=True)
        for slot_index, row in enumerate(candidates[:2]):
            slots[slot_index][time_index] = row
    return count, slots


def _select_top1_slot(arrays: "_FrameDets | list", frames: int):
    count = np.zeros(frames, dtype=np.float32)
    slot = np.zeros((frames, 5), dtype=np.float32)
    previous_center = None
    for time_index in range(frames):
        candidates = _frame_candidates(arrays, time_index)
        count[time_index] = len(candidates)
        if not candidates:
            continue
        candidates.sort(key=lambda row: _box_score(row, previous_center), reverse=True)
        slot[time_index] = candidates[0]
        previous_center = slot[time_index, 1:3]
    return count, slot


def _build_base_matrix(object_arrays: dict[str, list[np.ndarray]], frames: int, fps: float):
    blocks: list[np.ndarray] = []
    names: list[str] = []
    centers: dict[str, np.ndarray] = {}
    active: dict[str, np.ndarray] = {}

    hand_count, hand_slots = _select_hand_slots(object_arrays.get("hand", []), frames)
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
        count, slot = _select_top1_slot(object_arrays.get(object_name, []), frames)
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
