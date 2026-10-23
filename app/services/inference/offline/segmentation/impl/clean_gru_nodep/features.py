"""CLEAN nodep 检测框特征（ama-v3-concat23-nodep-226d：v2 ⊕ v3 拼接，剔除废弃类），原样迁自旧 impl/clean.py。

    model_input = build_nodep_concat_features(frames, fps, frame_width, frame_height, confidence_override)
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Dict, List, Sequence, Tuple

import numpy as np

from app.types.detection import DetBox, FrameDetection

from .contract import DEPRECATED_OBJECTS, FEATURE_DIM, FEATURE_VERSION

BASE_FEATURE_VERSION = "clean_bbox_v2_top1_impute"

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

OBJECT_ALIASES = {
    "hand": "hand",
    "short_brush": "short_brush",
    "long_brush": "long_brush",
    "syringe": "syringe",
    "air_gun": "air_gun",
    "scope_control_body": "scope_control_body",
    "scope_mid_section": "scope_mid_section",
    "scope_distal_end": "scope_distal_end",
    "brush_tip_out": "brush_tip_out",
}

PAIR_FEATURES = [
    ("hand", "short_brush"),
    ("hand", "long_brush"),
    ("brush_tip_out", "scope_distal_end"),
    ("short_brush", "scope_control_body"),
    ("long_brush", "scope_mid_section"),
    ("air_gun", "scope_distal_end"),
    ("syringe", "scope_distal_end"),
]


@dataclass(frozen=True)
class ModelInput:
    """clean 离线模型输入。

    features:
        [T, F] 数值特征矩阵。基础 v2 为 113 维；具体模型可在
        覆盖的 preprocess() 内扩展为 121/249 等模型专属输入。
    feature_names:
        features 每一列的名字，便于训练仓和后端排查对齐问题。
    timestamps:
        每一行特征对应的原始帧时间戳。
    fps:
        兜底采样率。speed 优先用真实 timestamp 的 dt 计算，dt 异常时才用 fps。
    feature_version:
        特征工程版本。加载 .pt 权重时必须和 checkpoint 内记录的 feature_version /
        feature_names 对齐，否则说明权重和后端输入不匹配。
    """

    features: List[List[float]]
    feature_names: List[str]
    timestamps: List[float]
    fps: float
    feature_version: str = BASE_FEATURE_VERSION

    @property
    def frame_count(self) -> int:
        return len(self.features)

    @property
    def feature_dim(self) -> int:
        return len(self.feature_names)


def build_base_features(
    frames: Sequence[FrameDetection],
    fps: float,
    frame_width: int = 640,
    frame_height: int = 480,
) -> ModelInput:
    """把 clean 帧级 FrameDetection 序列转换成 v2 固定维（113）时序特征。

    每帧 `FrameDetection.by_source` 里的多流检测在此按帧合并消费（无需上游先融合）。
    """
    frame_width = max(1, int(frame_width))
    frame_height = max(1, int(frame_height))
    timestamps = [ff.ts for ff in frames]  # FrameDetection.ts 已在 store.load 边界统一 float
    frame_count = len(frames)
    if frame_count <= 0:
        return ModelInput(features=[], feature_names=base_feature_names(), timestamps=[], fps=float(fps))

    effective_fps = _effective_fps(timestamps, float(fps))
    object_arrays = _collect_object_arrays(frames, frame_width, frame_height)
    features, names = _build_feature_matrix(object_arrays, frame_count, effective_fps)
    return ModelInput(
        features=features.tolist(),
        feature_names=names,
        timestamps=timestamps,
        fps=effective_fps,
        feature_version=BASE_FEATURE_VERSION,
    )


def base_feature_names() -> List[str]:
    """基础 v2 的 113 个特征列名（跑一遍空矩阵取名，避免维护第二份清单）。"""
    return _build_feature_matrix({name: {} for name in OBJECTS}, 1, 7.5)[1]


def _finite_matrix(values: np.ndarray) -> np.ndarray:
    """把 NaN/inf 兜底成 0，保持模型输入尺寸稳定且数值可送入 torch。"""
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def _collect_object_arrays(
    frames: Sequence[FrameDetection],
    frame_width: int,
    frame_height: int,
    confidence_override: float | None = None,
) -> Dict[str, Dict[int, List[np.ndarray]]]:
    """把每帧检测框按目标类别归拢成 {obj: {帧号: [每检测框一行 [5]]}}（帧内按检测顺序）。

    每帧遍历 `FrameDetection.by_source` 各流的检测（多流按帧合并，同 idx 落同一行）。
    `confidence_override` 非 None 时所有框置信度改用该值（对齐无置信度列的训练标注）。
    """
    out: Dict[str, Dict[int, List[np.ndarray]]] = {name: {} for name in OBJECTS}
    for idx, ff in enumerate(frames):
        # 帧级分辨率优先（pool 盖章、store 回读还原）；缺失回退传入默认。同帧各流同值。
        width = max(1, int(ff.frame_width or frame_width))
        height = max(1, int(ff.frame_height or frame_height))
        for fd in ff.by_source.values():
            for det in fd.boxes:
                obj = OBJECT_ALIASES.get(str(det.class_name))
                if obj is None:
                    continue
                cx, cy, area = _bbox_to_center_area(det, width, height)
                conf = det.confidence if confidence_override is None else confidence_override
                out[obj].setdefault(idx, []).append(np.array(
                    (1.0, float(cx), float(cy), float(area), max(0.0, min(1.0, float(conf)))),
                    dtype=np.float32,
                ))
    return out


def _effective_fps(timestamps: Sequence[float], fallback_fps: float) -> float:
    """用相邻帧 dt 的中位数估真实采样率；dt 不可用时回退 fallback_fps。"""
    if len(timestamps) < 2:
        return max(float(fallback_fps), 1e-6)
    deltas = [
        b - a for a, b in zip(timestamps[:-1], timestamps[1:])
        if math.isfinite(b - a) and (b - a) > 1e-6
    ]
    if not deltas:
        return max(float(fallback_fps), 1e-6)
    return max(1.0 / float(np.median(np.asarray(deltas, dtype=np.float32))), 1e-6)


def _bbox_to_center_area(det: DetBox, width: int, height: int) -> Tuple[float, float, float]:
    """xyxy 框空间归一化后返回 (中心 cx, 中心 cy, 面积)，坐标/面积均截到 [0,1]。"""
    if len(det.bbox) < 4:
        return 0.0, 0.0, 0.0
    x1, y1, x2, y2 = [float(v) for v in det.bbox[:4]]

    # detections.jsonl 当前保存的是 xyxy。若数值已经在 0-1，则按归一化坐标处理；
    # 否则按画面尺寸做空间归一化。
    normalized = max(abs(x1), abs(y1), abs(x2), abs(y2)) <= 1.5
    if normalized:
        nx1, ny1, nx2, ny2 = x1, y1, x2, y2
    else:
        nx1, ny1, nx2, ny2 = x1 / width, y1 / height, x2 / width, y2 / height

    nx1, nx2 = sorted((min(max(nx1, 0.0), 1.0), min(max(nx2, 0.0), 1.0)))
    ny1, ny2 = sorted((min(max(ny1, 0.0), 1.0), min(max(ny2, 0.0), 1.0)))
    bw = max(0.0, nx2 - nx1)
    bh = max(0.0, ny2 - ny1)
    return (nx1 + nx2) * 0.5, (ny1 + ny2) * 0.5, min(1.0, bw * bh)


def _as_box5(row: np.ndarray) -> np.ndarray:
    """把任意长度的行统一成 5 元 [present, cx, cy, area, conf]，不足补零。"""
    if row.shape[0] >= 5:
        return row[:5].astype(np.float32)
    out = np.zeros(5, dtype=np.float32)
    out[: min(4, row.shape[0])] = row[:4]
    out[4] = 1.0 if out[0] > 0 else 0.0
    return out


def _box_score(row: np.ndarray, prev_center: np.ndarray | None = None) -> float:
    """槽位竞争打分：conf×√area，再对偏离上一帧中心的位移做惩罚；缺框记 -1。"""
    present, cx, cy, area, conf = [float(x) for x in _as_box5(row)]
    if present <= 0:
        return -1.0
    score = conf * math.sqrt(max(area, 1e-6))
    if prev_center is not None:
        score -= 0.15 * min(math.dist((cx, cy), tuple(prev_center)), math.sqrt(2.0))
    return score


def _missing_age(raw_present: np.ndarray, max_gap: int) -> np.ndarray:
    """连续缺失帧数归一化到 [0,1]（越久没检到越接近 1），一旦命中清零。"""
    out = np.zeros(len(raw_present), dtype=np.float32)
    age = 0
    for idx, flag in enumerate(raw_present > 0):
        age = 0 if flag else age + 1
        out[idx] = min(age, max_gap) / max(1, max_gap)
    return out


def _impute_short_gaps(raw: np.ndarray, fps: float, max_gap: int = 6) -> Tuple[np.ndarray, np.ndarray]:
    """对短缺失段线性插值补帧，算出 speed 等 8 维；返回 (特征, active 掩码)。"""
    time_len = raw.shape[0]
    present = raw[:, 0].astype(np.float32)
    conf = raw[:, 4].astype(np.float32)
    cx = raw[:, 1].astype(np.float32).copy()
    cy = raw[:, 2].astype(np.float32).copy()
    area = raw[:, 3].astype(np.float32).copy()
    imputed = np.zeros(time_len, dtype=np.float32)

    detected = np.where(present > 0)[0]
    if len(detected):
        for left, right in zip(detected[:-1], detected[1:]):
            gap = int(right - left - 1)
            if 0 < gap <= max_gap:
                for offset, idx in enumerate(range(left + 1, right), start=1):
                    ratio = offset / (gap + 1)
                    cx[idx] = (1 - ratio) * cx[left] + ratio * cx[right]
                    cy[idx] = (1 - ratio) * cy[left] + ratio * cy[right]
                    area[idx] = (1 - ratio) * area[left] + ratio * area[right]
                    conf[idx] = 0.5 * ((1 - ratio) * conf[left] + ratio * conf[right])
                    imputed[idx] = 1.0
        last = int(detected[-1])
        tail_gap = min(max_gap, time_len - last - 1)
        for idx in range(last + 1, last + tail_gap + 1):
            cx[idx], cy[idx], area[idx] = cx[last], cy[last], area[last]
            conf[idx] = 0.5 * conf[last]
            imputed[idx] = 1.0

    active = (present > 0) | (imputed > 0)
    coords = np.stack([cx, cy], axis=1)
    speed = np.zeros(time_len, dtype=np.float32)
    if time_len > 1:
        speed[1:] = np.clip(np.linalg.norm(np.diff(coords, axis=0), axis=1) * fps, 0.0, 5.0) / 5.0
        speed[~active] = 0.0

    feature = np.stack(
        [present, conf, cx, cy, area, speed, _missing_age(present, max_gap), imputed],
        axis=1,
    ).astype(np.float32)
    feature[~active, 1:6] = 0.0
    return feature, active


def _select_hand_slots(hand_arrs: Dict[int, List[np.ndarray]], frames: int) -> Tuple[np.ndarray, List[np.ndarray]]:
    """每帧按打分取 hand 的 top-2 槽位（双手），并返回逐帧 hand 计数。"""
    hand_count = np.zeros(frames, dtype=np.float32)
    slots = [np.zeros((frames, 5), dtype=np.float32), np.zeros((frames, 5), dtype=np.float32)]
    for t in range(frames):
        candidates = [_as_box5(row) for row in hand_arrs.get(t, ()) if _as_box5(row)[0] > 0]
        hand_count[t] = len(candidates)
        candidates.sort(key=lambda row: _box_score(row), reverse=True)
        for slot_idx, row in enumerate(candidates[:2]):
            slots[slot_idx][t] = row
    return hand_count, slots


def _select_top1_slot(arrs: Dict[int, List[np.ndarray]], frames: int) -> Tuple[np.ndarray, np.ndarray]:
    """每帧取单目标 top-1 槽位（带上一帧中心做时序连续性打分），返回计数与槽位。"""
    count = np.zeros(frames, dtype=np.float32)
    slot = np.zeros((frames, 5), dtype=np.float32)
    prev_center: np.ndarray | None = None
    for t in range(frames):
        candidates = [_as_box5(row) for row in arrs.get(t, ()) if _as_box5(row)[0] > 0]
        count[t] = len(candidates)
        if not candidates:
            continue
        candidates.sort(key=lambda row: _box_score(row, prev_center), reverse=True)
        slot[t] = candidates[0]
        prev_center = slot[t, 1:3]
    return count, slot


def _build_feature_matrix(
    object_arrays: Dict[str, Dict[int, List[np.ndarray]]],
    frames: int,
    fps: float,
) -> Tuple[np.ndarray, List[str]]:
    """拼装完整 v2 矩阵：hand top-2 + 各目标 top-1 + 关键目标对 + 时间编码，返回 (矩阵, 列名)。"""
    blocks: List[np.ndarray] = []
    names: List[str] = []
    centers: Dict[str, np.ndarray] = {}
    active: Dict[str, np.ndarray] = {}

    hand_count, hand_slots = _select_hand_slots(object_arrays.get("hand", {}), frames)
    blocks.append((np.clip(hand_count, 0, 3) / 3.0)[:, None].astype(np.float32))
    names.append("hand_count")
    hand_centers = []
    hand_active = []
    for slot_idx, slot in enumerate(hand_slots, start=1):
        feature, slot_active = _impute_short_gaps(slot, fps)
        blocks.append(feature)
        names += [
            f"hand_top{slot_idx}_present",
            f"hand_top{slot_idx}_conf",
            f"hand_top{slot_idx}_cx",
            f"hand_top{slot_idx}_cy",
            f"hand_top{slot_idx}_area",
            f"hand_top{slot_idx}_speed",
            f"hand_top{slot_idx}_missing_age",
            f"hand_top{slot_idx}_imputed",
        ]
        hand_centers.append(feature[:, 2:4])
        hand_active.append(slot_active)
    centers["hand"] = np.stack(hand_centers, axis=0)
    active["hand"] = np.logical_or.reduce(hand_active) if hand_active else np.zeros(frames, dtype=bool)

    for obj in OBJECTS:
        if obj == "hand":
            continue
        count, slot = _select_top1_slot(object_arrays.get(obj, {}), frames)
        feature, obj_active = _impute_short_gaps(slot, fps)
        blocks.append(np.concatenate([(np.clip(count, 0, 3) / 3.0)[:, None], feature], axis=1).astype(np.float32))
        names += [
            f"{obj}_candidate_count",
            f"{obj}_present",
            f"{obj}_conf",
            f"{obj}_cx",
            f"{obj}_cy",
            f"{obj}_area",
            f"{obj}_speed",
            f"{obj}_missing_age",
            f"{obj}_imputed",
        ]
        centers[obj] = feature[:, 2:4]
        active[obj] = obj_active

    for left, right in PAIR_FEATURES:
        valid = (active[left] & active[right]).astype(np.float32)
        if left == "hand":
            d0 = np.linalg.norm(centers["hand"][0] - centers[right], axis=1)
            d1 = np.linalg.norm(centers["hand"][1] - centers[right], axis=1)
            dist = np.minimum(d0, d1).astype(np.float32)
        elif right == "hand":
            d0 = np.linalg.norm(centers[left] - centers["hand"][0], axis=1)
            d1 = np.linalg.norm(centers[left] - centers["hand"][1], axis=1)
            dist = np.minimum(d0, d1).astype(np.float32)
        else:
            dist = np.linalg.norm(centers[left] - centers[right], axis=1).astype(np.float32)
        dist = np.where(valid > 0, np.clip(dist, 0.0, math.sqrt(2.0)) / math.sqrt(2.0), 0.0)
        delta = np.zeros(frames, dtype=np.float32)
        if frames > 1:
            delta[1:] = np.clip(dist[1:] - dist[:-1], -1.0, 1.0)
            delta[valid <= 0] = 0.0
        blocks.append(np.stack([valid, dist, delta], axis=1).astype(np.float32))
        names += [f"{left}_to_{right}_valid", f"{left}_to_{right}_dist", f"{left}_to_{right}_delta"]

    t = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    blocks.append(np.stack([t, np.sin(2 * np.pi * t), np.cos(2 * np.pi * t)], axis=1).astype(np.float32))
    names += ["t_norm", "t_sin", "t_cos"]
    return _finite_matrix(np.concatenate(blocks, axis=1)), names


# -------------------- clean_bbox_v3_scope_frame（113 维，scope 器械轴坐标系） --------------------
#
# 与训练框架 `clean_bbox_v3.py` 逐函数对齐：槽位选择 / 短缺失插值复用上面 v2 的实现，
# 位置投影到 control_body→distal_end 轴（缺席回退 mid_section），面积取相对 distal_end 的对数比。

_V3_CLIP_POS = 4.0
_V3_CLIP_LOG = 6.0
_V3_FALLBACK_REF_LEN = 0.35
_V3_FALLBACK_REF_AREA = 0.02
_V3_SLOT_CHANNELS = ["present", "conf", "along", "across", "log_area_ratio", "speed", "missing_age", "imputed"]


def _forward_fill(values: np.ndarray, valid: np.ndarray, fallback: float) -> np.ndarray:
    """沿时间前向填充有效值；序列内无任何有效值时全部回退 fallback。"""
    out = np.full(len(values), float(fallback), dtype=np.float32)
    last = None
    for idx in range(len(values)):
        if valid[idx]:
            last = float(values[idx])
        if last is not None:
            out[idx] = last
    return out


def _scope_frame(
    features: Dict[str, np.ndarray], active: Dict[str, np.ndarray], frames: int
) -> Tuple[np.ndarray, np.ndarray, np.ndarray]:
    """由 scope 三点构造逐帧 (单位轴向量, 轴长 ref_len, 参考面积 ref_area)。"""
    control = features["scope_control_body"][:, 2:4]
    mid = features["scope_mid_section"][:, 2:4]
    distal = features["scope_distal_end"][:, 2:4]
    active_ctl = active["scope_control_body"]
    active_mid = active["scope_mid_section"]
    active_dis = active["scope_distal_end"]

    axes = np.zeros((frames, 2), dtype=np.float32)
    ref_len = np.full(frames, _V3_FALLBACK_REF_LEN, dtype=np.float32)
    last_axis = None
    valid_lens: List[float] = []
    len_valid = np.zeros(frames, dtype=bool)
    for idx in range(frames):
        for left, right, left_ok, right_ok in (
            (control[idx], distal[idx], active_ctl[idx], active_dis[idx]),
            (mid[idx], distal[idx], active_mid[idx], active_dis[idx]),
            (control[idx], mid[idx], active_ctl[idx], active_mid[idx]),
        ):
            if not (left_ok and right_ok):
                continue
            vector = right - left
            length = float(np.linalg.norm(vector))
            if length <= 1e-4:
                continue
            last_axis = (vector / length).astype(np.float32)
            ref_len[idx] = length
            len_valid[idx] = True
            valid_lens.append(length)
            break
        axes[idx] = last_axis if last_axis is not None else np.array([1.0, 0.0], np.float32)
    if valid_lens:
        median_len = float(np.median(valid_lens))
        ref_len = np.where(len_valid, ref_len, _forward_fill(ref_len, len_valid, median_len)).astype(np.float32)

    area_values = np.where(active_dis, features["scope_distal_end"][:, 4], np.nan).astype(np.float32)
    if active_dis.any():
        median_area = float(np.nanmedian(area_values))
        ref_area = np.where(
            active_dis,
            area_values,
            _forward_fill(np.nan_to_num(area_values, nan=median_area), active_dis, median_area),
        ).astype(np.float32)
    else:
        ref_area = np.full(frames, _V3_FALLBACK_REF_AREA, dtype=np.float32)
    return axes, ref_len, ref_area


def _v3_slot_channels(
    feature: np.ndarray,
    active: np.ndarray,
    axes: np.ndarray,
    ref_len: np.ndarray,
    ref_area: np.ndarray,
    control: np.ndarray,
) -> np.ndarray:
    """把 v2 插值特征 `[T,8]` 转成 v3 的 8 通道 scope 坐标特征。"""
    relative = feature[:, 2:4] - control
    along = np.einsum("td,td->t", relative, axes) / ref_len
    perpendicular = np.stack([-axes[:, 1], axes[:, 0]], axis=1)
    across = np.einsum("td,td->t", relative, perpendicular) / ref_len
    log_ratio = np.log((feature[:, 4] + 1e-4) / (ref_area + 1e-4))
    channels = np.stack(
        [
            feature[:, 0],
            feature[:, 1],
            np.clip(along, -_V3_CLIP_POS, _V3_CLIP_POS),
            np.clip(across, -_V3_CLIP_POS, _V3_CLIP_POS),
            np.clip(log_ratio, -_V3_CLIP_LOG, _V3_CLIP_LOG),
            feature[:, 5],
            feature[:, 6],
            feature[:, 7],
        ],
        axis=1,
    ).astype(np.float32)
    channels[~active, 1:6] = 0.0
    return channels


def _build_v3_matrix(
    object_arrays: Dict[str, Dict[int, List[np.ndarray]]], frames: int, fps: float
) -> Tuple[np.ndarray, List[str]]:
    """拼装 v3 base 矩阵 `[T,113]`，块布局与 v2 base 一一对应，返回 (矩阵, 列名)。"""
    features: Dict[str, np.ndarray] = {}
    active: Dict[str, np.ndarray] = {}
    counts: Dict[str, np.ndarray] = {}

    hand_count, hand_slots = _select_hand_slots(object_arrays.get("hand", {}), frames)
    for slot_idx, slot in enumerate(hand_slots, start=1):
        features[f"hand_top{slot_idx}"], active[f"hand_top{slot_idx}"] = _impute_short_gaps(slot, fps)
    for obj in OBJECTS:
        if obj == "hand":
            continue
        counts[obj], slot = _select_top1_slot(object_arrays.get(obj, {}), frames)
        features[obj], active[obj] = _impute_short_gaps(slot, fps)

    axes, ref_len, ref_area = _scope_frame(features, active, frames)
    control = features["scope_control_body"][:, 2:4]

    blocks: List[np.ndarray] = [(np.clip(hand_count, 0, 3) / 3.0)[:, None].astype(np.float32)]
    names: List[str] = ["hand_count"]
    for key in ("hand_top1", "hand_top2"):
        blocks.append(_v3_slot_channels(features[key], active[key], axes, ref_len, ref_area, control))
        names += [f"{key}_{ch}" for ch in _V3_SLOT_CHANNELS]
    for obj in OBJECTS:
        if obj == "hand":
            continue
        blocks.append(np.concatenate(
            [
                (np.clip(counts[obj], 0, 3) / 3.0)[:, None].astype(np.float32),
                _v3_slot_channels(features[obj], active[obj], axes, ref_len, ref_area, control),
            ],
            axis=1,
        ))
        names.append(f"{obj}_candidate_count")
        names += [f"{obj}_{ch}" for ch in _V3_SLOT_CHANNELS]

    for left, right in PAIR_FEATURES:
        left_keys = ["hand_top1", "hand_top2"] if left == "hand" else [left]
        right_keys = ["hand_top1", "hand_top2"] if right == "hand" else [right]
        valid = np.zeros(frames, dtype=np.float32)
        dist = np.full(frames, np.inf, dtype=np.float32)
        for lk in left_keys:
            for rk in right_keys:
                pair_valid = (active[lk] & active[rk]).astype(np.float32)
                pair_dist = np.linalg.norm(features[lk][:, 2:4] - features[rk][:, 2:4], axis=1)
                pick = (pair_valid > 0) & (pair_dist < dist)
                valid[pick] = 1.0
                dist[pick] = pair_dist[pick]
        dist = np.where(valid > 0, np.clip(dist, 0.0, math.sqrt(2.0)) / math.sqrt(2.0), 0.0).astype(np.float32)
        delta = np.zeros(frames, dtype=np.float32)
        if frames > 1:
            delta[1:] = np.clip(dist[1:] - dist[:-1], -1.0, 1.0)
            delta[valid <= 0] = 0.0
        blocks.append(np.stack([valid, dist, delta], axis=1).astype(np.float32))
        names += [f"{left}_to_{right}_valid", f"{left}_to_{right}_dist", f"{left}_to_{right}_delta"]

    t = np.linspace(0.0, 1.0, frames, dtype=np.float32)
    blocks.append(np.stack([t, np.sin(2 * np.pi * t), np.cos(2 * np.pi * t)], axis=1).astype(np.float32))
    names += ["t_norm", "t_sin", "t_cos"]
    return _finite_matrix(np.concatenate(blocks, axis=1)), names


# -------------------- ama-v3-concat23-nodep-226d（v2 ⊕ v3，剔除废弃类） --------------------



def build_nodep_concat_features(
    frames: Sequence[FrameDetection],
    fps: float,
    frame_width: int = 640,
    frame_height: int = 480,
    confidence_override: float | None = None,
) -> ModelInput:
    """v2 ⊕ v3 同编码拼接 `[T,226]`；废弃类检测在读入层丢弃（对应块全零）。

    `fps` 用模型契约帧率（训练框架以固定 fps 算 speed），不从 ts 估计。
    列名加 `v2.` / `v3.` 前缀区分两半（两半同名列很多）。
    """
    frame_count = len(frames)
    if frame_count <= 0:
        return ModelInput(
            features=[], feature_names=[], timestamps=[], fps=float(fps), feature_version=FEATURE_VERSION,
        )
    object_arrays = _collect_object_arrays(
        frames, max(1, int(frame_width)), max(1, int(frame_height)), confidence_override,
    )
    for obj in DEPRECATED_OBJECTS:
        object_arrays[obj] = {}
    timestamps = [ff.ts for ff in frames]
    v2, v2_names = _build_feature_matrix(object_arrays, frame_count, float(fps))
    v3, v3_names = _build_v3_matrix(object_arrays, frame_count, float(fps))
    features = np.concatenate([v2, v3], axis=1).astype(np.float32)
    if features.shape[1] != FEATURE_DIM:
        raise AssertionError(f"nodep 拼接维度 {features.shape[1]} != {FEATURE_DIM}")
    return ModelInput(
        features=features.tolist(),
        feature_names=[f"v2.{n}" for n in v2_names] + [f"v3.{n}" for n in v3_names],
        timestamps=timestamps,
        fps=float(fps),
        feature_version=FEATURE_VERSION,
    )
