"""CLEAN stage 离线模型策略。

本文件保持“单策略文件自包含”：
    - clean 专属特征转换（模块级纯函数：v2 / v3 / nodep 拼接及 recipe）；
    - 离线模型结构（三种整段模型 + 因果滑窗 GRU）；
    - 模型输出到 TemporalSegment 的解码逻辑。

输入:
    OfflineRunner 从 inference.read_detections(task_id, step_id) 读取 List[FrameDetection]
    （帧级、多流已在 by_source 内对齐、按 ts 升序）。

输出:
    List[TemporalSegment]，由 Runner 校验并幂等写入 temporal.jsonl。

注意:
    这里不包含训练流程。训练仍在独立 offline-model 仓内完成，后端只负责加载
    已训练权重并执行离线推理。若未配置 model_path，CLEAN 模型会硬失败。
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from app.domain.detection import DetBox, FrameDetection
from app.domain.temporal import LabelProbs, TemporalSegment
from app.services.inference.offline.segmenter import OfflineSegmenter
from app.services.inference.resample import resample_by_ts


FEATURE_VERSION = "clean_bbox_v2_top1_impute"

ACTION_LABELS = [
    "idle",
    "long_brush_insert",
    "long_brush_withdraw",
    "short_brush_cleaning",
    "flush",
    "air_injection",
]

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
    feature_version: str = FEATURE_VERSION

    @property
    def frame_count(self) -> int:
        return len(self.features)

    @property
    def feature_dim(self) -> int:
        return len(self.feature_names)


# ==================== 特征工程（模块级纯函数） ====================
#
# clean 检测框序列 -> v2 固定维时序特征，与 offline-model 的
# `clean_bbox_v2_top1_impute` 对齐：
#     - hand 使用 top-2 槽位；
#     - 其它目标使用 top-1，不做同类多框加权平均；
#     - 每个目标包含 present/conf/cx/cy/area/speed/missing_age/imputed；
#     - 对关键目标对补 valid/dist/delta；
#     - 最后补时间位置编码。
#
# 这些是无状态函数（原 FeatureVectorizer 类无跨调用状态，只是命名空间）。窗口统计、
# 业务先验等模型专属增强由各模型在覆盖的 preprocess() 内自行叠加。


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
        feature_version=FEATURE_VERSION,
    )


def base_feature_names() -> List[str]:
    """基础 v2 的 113 个特征列名（跑一遍空矩阵取名，避免维护第二份清单）。"""
    return _build_feature_matrix({name: [] for name in OBJECTS}, 1, 7.5)[1]


def _finite_matrix(values: np.ndarray) -> np.ndarray:
    """把 NaN/inf 兜底成 0，保持模型输入尺寸稳定且数值可送入 torch。"""
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def _collect_object_arrays(
    frames: Sequence[FrameDetection],
    frame_width: int,
    frame_height: int,
    confidence_override: float | None = None,
) -> Dict[str, List[np.ndarray]]:
    """把每帧检测框按目标类别归拢成 {obj: [每检测框一个 [T,5] 稀疏数组]}。

    每帧遍历 `FrameDetection.by_source` 各流的检测（多流按帧合并，同 idx 落同一行）。
    `confidence_override` 非 None 时所有框置信度改用该值（对齐无置信度列的训练标注）。
    """
    frame_count = len(frames)
    out: Dict[str, List[np.ndarray]] = {name: [] for name in OBJECTS}
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
                arr = np.zeros((frame_count, 5), dtype=np.float32)
                arr[idx] = (
                    1.0,
                    float(cx),
                    float(cy),
                    float(area),
                    max(0.0, min(1.0, float(conf))),
                )
                out[obj].append(arr)
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


def _select_hand_slots(hand_arrs: List[np.ndarray], frames: int) -> Tuple[np.ndarray, List[np.ndarray]]:
    """每帧按打分取 hand 的 top-2 槽位（双手），并返回逐帧 hand 计数。"""
    hand_count = np.zeros(frames, dtype=np.float32)
    slots = [np.zeros((frames, 5), dtype=np.float32), np.zeros((frames, 5), dtype=np.float32)]
    for t in range(frames):
        candidates = [_as_box5(arr[t]) for arr in hand_arrs if _as_box5(arr[t])[0] > 0]
        hand_count[t] = len(candidates)
        candidates.sort(key=lambda row: _box_score(row), reverse=True)
        for slot_idx, row in enumerate(candidates[:2]):
            slots[slot_idx][t] = row
    return hand_count, slots


def _select_top1_slot(arrs: List[np.ndarray], frames: int) -> Tuple[np.ndarray, np.ndarray]:
    """每帧取单目标 top-1 槽位（带上一帧中心做时序连续性打分），返回计数与槽位。"""
    count = np.zeros(frames, dtype=np.float32)
    slot = np.zeros((frames, 5), dtype=np.float32)
    prev_center: np.ndarray | None = None
    for t in range(frames):
        candidates = [_as_box5(arr[t]) for arr in arrs if _as_box5(arr[t])[0] > 0]
        count[t] = len(candidates)
        if not candidates:
            continue
        candidates.sort(key=lambda row: _box_score(row, prev_center), reverse=True)
        slot[t] = candidates[0]
        prev_center = slot[t, 1:3]
    return count, slot


def _build_feature_matrix(
    object_arrays: Dict[str, List[np.ndarray]],
    frames: int,
    fps: float,
) -> Tuple[np.ndarray, List[str]]:
    """拼装完整 v2 矩阵：hand top-2 + 各目标 top-1 + 关键目标对 + 时间编码，返回 (矩阵, 列名)。"""
    blocks: List[np.ndarray] = []
    names: List[str] = []
    centers: Dict[str, np.ndarray] = {}
    active: Dict[str, np.ndarray] = {}

    hand_count, hand_slots = _select_hand_slots(object_arrays.get("hand", []), frames)
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
        count, slot = _select_top1_slot(object_arrays.get(obj, []), frames)
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


# -------------------- 模型专属特征 recipe（供子类覆盖的 preprocess 调用） --------------------


def _with_features(model_input: ModelInput, features: np.ndarray, names: List[str], version: str) -> ModelInput:
    """基于原 ModelInput 换一套特征/列名/版本，重建新 ModelInput（含 finite 兜底）。"""
    features = _finite_matrix(features)
    return ModelInput(
        features=features.tolist(),
        feature_names=names,
        timestamps=list(model_input.timestamps),
        fps=float(model_input.fps),
        feature_version=version,
    )


def _centered_mean(values: np.ndarray, radius: int) -> np.ndarray:
    """以每帧为中心、半径 radius 的居中滑窗均值（边界收缩窗口，不补零）。"""
    if radius <= 0:
        return values.astype(np.float32)
    out = np.zeros_like(values, dtype=np.float32)
    for idx in range(len(values)):
        lo = max(0, idx - radius)
        hi = min(len(values), idx + radius + 1)
        out[idx] = values[lo:hi].mean(axis=0)
    return out


def add_centered_window_stats(model_input: ModelInput, windows: Tuple[int, ...] = (5, 15)) -> ModelInput:
    """recipe：对 present/conf/speed 等列追加多尺度居中滑窗均值（BiGRU 用）。"""
    feature = np.asarray(model_input.features, dtype=np.float32)
    names = list(model_input.feature_names)
    selected = [
        idx for idx, name in enumerate(names)
        if name.endswith(("_present", "_conf", "_speed", "_dist", "_delta", "_missing_age", "_imputed"))
    ]
    if not selected:
        return model_input

    base = feature[:, selected]
    extra_blocks: List[np.ndarray] = []
    extra_names: List[str] = []
    for window in windows:
        radius = max(1, window // 2)
        mean = _centered_mean(base, radius)
        extra_blocks.append(mean)
        extra_names.extend([f"{names[idx]}_center_mean_w{window}" for idx in selected])
    out = np.concatenate([feature, *extra_blocks], axis=1).astype(np.float32)
    return _with_features(
        model_input,
        out,
        names + extra_names,
        f"{model_input.feature_version}+center_window",
    )


def _col(features: np.ndarray, name_to_idx: Dict[str, int], name: str) -> np.ndarray:
    """按列名取一整列；列不存在则返回全零（缺列→补零的统一不变式）。"""
    idx = name_to_idx.get(name)
    if idx is None:
        return np.zeros(features.shape[0], dtype=np.float32)
    return features[:, idx].astype(np.float32)


def _near_score(dist: np.ndarray) -> np.ndarray:
    """把归一化距离翻成 [0,1] 的接近度（越近越接近 1）。"""
    return np.clip(1.0 - dist, 0.0, 1.0).astype(np.float32)


def add_business_priors(model_input: ModelInput) -> ModelInput:
    """recipe：按业务规则叠加 8 维动作先验（接近度×存在×运动等），ASFormer/BiGRU 用。"""
    x = np.asarray(model_input.features, dtype=np.float32)
    names = list(model_input.feature_names)
    n = {name: idx for idx, name in enumerate(names)}

    hand = np.maximum(_col(x, n, "hand_top1_present"), _col(x, n, "hand_top2_present"))
    short_brush = _col(x, n, "short_brush_present")
    syringe = _col(x, n, "syringe_present")
    air_gun = _col(x, n, "air_gun_present")
    brush_tip = _col(x, n, "brush_tip_out_present")
    long_brush = _col(x, n, "long_brush_present")

    short_near = _near_score(_col(x, n, "short_brush_to_scope_control_body_dist"))
    syringe_near = _near_score(_col(x, n, "syringe_to_scope_distal_end_dist"))
    air_near = _near_score(_col(x, n, "air_gun_to_scope_distal_end_dist"))
    tip_near = _near_score(_col(x, n, "brush_tip_out_to_scope_distal_end_dist"))
    long_near = _near_score(_col(x, n, "long_brush_to_scope_mid_section_dist"))

    short_motion = np.maximum(
        _col(x, n, "short_brush_speed"),
        np.abs(_col(x, n, "short_brush_to_scope_control_body_delta")),
    )
    syringe_stable = syringe * syringe_near * (1.0 - np.clip(_col(x, n, "syringe_speed"), 0.0, 1.0))
    air_stable = air_gun * air_near * (1.0 - np.clip(_col(x, n, "air_gun_speed"), 0.0, 1.0))
    long_signal = np.maximum.reduce([long_brush, brush_tip, _col(x, n, "brush_tip_out_imputed")])
    long_delta = _col(x, n, "brush_tip_out_to_scope_distal_end_delta")
    hand_to_long = _near_score(_col(x, n, "hand_to_long_brush_dist"))

    priors = np.stack(
        [
            hand * short_brush * short_near,
            hand * short_brush * short_motion,
            hand * syringe_stable,
            hand * air_stable,
            hand * long_signal * np.maximum(tip_near, long_near),
            hand * long_signal * np.clip(-long_delta, 0.0, 1.0),
            hand * long_signal * np.clip(long_delta, 0.0, 1.0),
            hand_to_long * long_signal,
        ],
        axis=1,
    ).astype(np.float32)
    prior_names = [
        "prior_short_clean_near",
        "prior_short_clean_motion",
        "prior_flush_stable",
        "prior_air_stable",
        "prior_long_signal_near_scope",
        "prior_long_towards_distal",
        "prior_long_away_distal",
        "prior_hand_long_contact",
    ]
    return _with_features(
        model_input,
        np.concatenate([x, priors], axis=1).astype(np.float32),
        names + prior_names,
        f"{model_input.feature_version}+business_priors",
    )


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
    object_arrays: Dict[str, List[np.ndarray]], frames: int, fps: float
) -> Tuple[np.ndarray, List[str]]:
    """拼装 v3 base 矩阵 `[T,113]`，块布局与 v2 base 一一对应，返回 (矩阵, 列名)。"""
    features: Dict[str, np.ndarray] = {}
    active: Dict[str, np.ndarray] = {}
    counts: Dict[str, np.ndarray] = {}

    hand_count, hand_slots = _select_hand_slots(object_arrays.get("hand", []), frames)
    for slot_idx, slot in enumerate(hand_slots, start=1):
        features[f"hand_top{slot_idx}"], active[f"hand_top{slot_idx}"] = _impute_short_gaps(slot, fps)
    for obj in OBJECTS:
        if obj == "hand":
            continue
        counts[obj], slot = _select_top1_slot(object_arrays.get(obj, []), frames)
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

NODEP_FEATURE_VERSION = "ama-v3-concat23-nodep-226d"
NODEP_FEATURE_DIM = 226
NODEP_DEPRECATED_OBJECTS = ("scope_distal_end", "short_brush", "long_brush")


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
            features=[], feature_names=[], timestamps=[], fps=float(fps), feature_version=NODEP_FEATURE_VERSION,
        )
    object_arrays = _collect_object_arrays(
        frames, max(1, int(frame_width)), max(1, int(frame_height)), confidence_override,
    )
    for obj in NODEP_DEPRECATED_OBJECTS:
        object_arrays[obj] = []
    timestamps = [ff.ts for ff in frames]
    v2, v2_names = _build_feature_matrix(object_arrays, frame_count, float(fps))
    v3, v3_names = _build_v3_matrix(object_arrays, frame_count, float(fps))
    features = np.concatenate([v2, v3], axis=1).astype(np.float32)
    if features.shape[1] != NODEP_FEATURE_DIM:
        raise AssertionError(f"nodep 拼接维度 {features.shape[1]} != {NODEP_FEATURE_DIM}")
    return ModelInput(
        features=features.tolist(),
        feature_names=[f"v2.{n}" for n in v2_names] + [f"v3.{n}" for n in v3_names],
        timestamps=timestamps,
        fps=float(fps),
        feature_version=NODEP_FEATURE_VERSION,
    )


class _CleanTorchSegmenter(OfflineSegmenter):
    """clean 模型策略基类：torch 模型加载 + 推理 + TemporalSegment 解码。

    特征工程是模块级纯函数：`preprocess` 调 build_base_features 得基础 v2（113 维）；
    需叠加模型专属 recipe 的子类**覆盖 preprocess**，用 `super().preprocess()` 取基础特征后
    再调模块级特征函数（add_business_priors / add_centered_window_stats）。
    """

    model_version = "clean_model_v1"
    feature_method = "v2"
    # 模型输出列序 → 动作名（与权重一一对应）；id 0 须为背景类，解码时跳过
    labels: Tuple[str, ...] = tuple(ACTION_LABELS)

    def __init__(
        self,
        model_path: str | None = None,
        min_duration_s: float = 0.2,
        fps: float = 7.5,
        frame_width: int = 640,
        frame_height: int = 480,
    ):
        self.model_path = model_path
        self.min_duration_s = max(0.0, float(min_duration_s))
        self.fps = float(fps)
        self.frame_width = max(1, int(frame_width))
        self.frame_height = max(1, int(frame_height))
        self._model = None
        self._normalizer: Tuple[Any, Any] | None = None
        self._last_probs: LabelProbs | None = None

    def preprocess(self, frames: Sequence[FrameDetection]) -> ModelInput:
        """帧级 FrameDetection 序列 → 基础 v2 特征（113 维）。

        多流按帧合并折进 build_base_features（`frames` 已按 ts 升序、各流在 by_source 内对齐）。
        需叠加模型专属 recipe 的子类覆盖本方法，用 `super().preprocess()` 取基础特征后再变换。
        """
        return build_base_features(frames, self.fps, self.frame_width, self.frame_height)

    def segment(self, model_input: ModelInput) -> List[TemporalSegment]:
        """跑模型得到逐帧标签，解码成 TemporalSegment；未配 model_path 硬失败，不做规则降级。"""
        if model_input.frame_count == 0:
            return []

        if not self.model_path:
            raise ValueError(
                f"{type(self).__name__} 未配置 model_path；CLEAN 离线模型不做规则降级"
            )

        probs = self._predict_with_model(model_input)
        labels = probs.argmax(axis=1).astype("int64").tolist()
        confs = probs.max(axis=1).astype("float32").tolist()

        segments = self._labels_to_segments(model_input.timestamps, labels, confs)
        self._last_probs = LabelProbs(
            ts=np.asarray(model_input.timestamps, dtype=np.float64),
            probs=probs,
            labels=self.labels,
        )
        return segments

    def label_probs(self) -> LabelProbs | None:
        """最近一次 segment() 的逐帧 softmax（未跑过为 None），供可视化旁路落盘。"""
        return self._last_probs

    def _predict_with_model(self, model_input: ModelInput) -> np.ndarray:
        """惰性加载权重，归一化+finite 兜底后前向，返回逐帧 softmax `[T, len(self.labels)]`。"""
        import numpy as np
        import torch

        if self._model is None:
            self._load_model(model_input, len(self.labels))

        x_np = np.asarray(model_input.features, dtype=np.float32)
        if self._normalizer is not None:
            mean, std = self._normalizer
            x_np = (x_np - mean) / std
        x_np = np.nan_to_num(x_np, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)
        x = torch.tensor(x_np[None, :, :], dtype=torch.float32)
        self._model.eval()
        with torch.no_grad():
            logits = self._model(x)[0].transpose(0, 1)
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
        return probs.astype(np.float32)

    def _load_model(self, model_input: ModelInput, class_count: int) -> None:
        """加载 .pt checkpoint 并校验 feature_names/feature_version 与后端输入一致，取出 normalizer。"""
        import torch

        path = Path(str(self.model_path))
        if not path.exists():
            raise FileNotFoundError(f"clean 离线模型权重不存在: {path}")

        try:
            # PyTorch 2.6 起 torch.load 默认 weights_only=True，会拒绝包含 numpy
            # normalizer 的可信训练 checkpoint；这里加载的是本地 offline-model 产物。
            checkpoint = torch.load(path, map_location="cpu", weights_only=False)
        except TypeError:
            checkpoint = torch.load(path, map_location="cpu")
        in_dim = model_input.feature_dim
        self._model = self._build_model(in_dim, class_count)
        state_dict = checkpoint.get("state_dict", checkpoint)
        self._model.load_state_dict(state_dict, strict=True)

        feature_names = checkpoint.get("feature_names")
        if feature_names is not None and list(feature_names) != list(model_input.feature_names):
            raise ValueError("clean 离线模型 feature_names 与后端特征列不一致")
        feature_version = checkpoint.get("feature_version")
        if isinstance(feature_version, str):
            ckpt_version = feature_version
        elif feature_version is not None and len(feature_version):
            ckpt_version = str(feature_version[0])
        else:
            ckpt_version = None
        if ckpt_version is not None and ckpt_version != model_input.feature_version:
            raise ValueError(
                f"clean 离线模型 feature_version 不一致: checkpoint={ckpt_version}, input={model_input.feature_version}"
            )

        mean = checkpoint.get("normalizer_mean")
        std = checkpoint.get("normalizer_std")
        if mean is not None and std is not None:
            self._normalizer = (mean, std)

    def _labels_to_segments(
        self, timestamps: Sequence[float], labels: Sequence[int], confs: Sequence[float]
    ) -> List[TemporalSegment]:
        """把逐帧标签合并成连续动作段（跳过 idle、过滤短于 min_duration_s 的段）。"""
        segments: List[TemporalSegment] = []
        cur_label: int | None = None
        cur_start = cur_end = 0.0
        cur_conf = 0.0
        cur_count = 0

        def flush() -> None:
            nonlocal cur_label, cur_conf, cur_count
            if cur_label is not None and cur_label != 0 and (cur_end - cur_start) >= self.min_duration_s:
                segments.append(TemporalSegment(
                    producer=self.name,
                    label=self.labels[cur_label],
                    start=round(cur_start, 6),
                    end=round(cur_end, 6),
                    conf=min(1.0, max(0.0, cur_conf / max(cur_count, 1))),
                    meta={"model_version": self.model_version},
                ))
            cur_label = None
            cur_conf = 0.0
            cur_count = 0

        for ts, label, conf in zip(timestamps, labels, confs):
            label = int(label)
            if label == 0:
                flush()
                continue
            if cur_label != label:
                flush()
                cur_label = label
                cur_start = cur_end = float(ts)
                cur_conf = float(conf)
                cur_count = 1
            else:
                cur_end = float(ts)
                cur_conf += float(conf)
                cur_count += 1
        flush()
        return segments

    def _build_model(self, in_dim: int, class_count: int):
        """构建本模型的 torch 网络（子类按自身结构实现）。"""
        raise NotImplementedError


# ==================== 三种 clean 离线模型结构 ====================


def _make_mstcn_bilstm(in_dim: int, class_count: int, hidden: int = 64):
    """构建 MS-TCN + BiLSTM 网络（BiLSTM 编码 → 单阶段 TCN → 两级 refine）。"""
    import torch
    import torch.nn as nn

    class DilatedResidualLayer(nn.Module):
        def __init__(self, channels: int, dilation: int, dropout: float):
            super().__init__()
            self.conv_dilated = nn.Conv1d(
                channels,
                channels,
                kernel_size=3,
                padding=dilation,
                dilation=dilation,
            )
            self.conv_1x1 = nn.Conv1d(channels, channels, kernel_size=1)
            self.norm = nn.BatchNorm1d(channels)
            self.dropout = nn.Dropout(dropout)
            self.act = nn.ReLU()

        def forward(self, x):
            out = self.conv_dilated(x)
            out = self.act(self.norm(out))
            out = self.conv_1x1(out)
            out = self.dropout(out)
            return self.act(x + out)

    class SingleStageTCN(nn.Module):
        def __init__(self, in_channels: int, classes: int, hidden: int, layers: int, dropout: float):
            super().__init__()
            self.input_projection = nn.Conv1d(in_channels, hidden, kernel_size=1)
            self.layers = nn.ModuleList(
                DilatedResidualLayer(hidden, dilation=2 ** i, dropout=dropout)
                for i in range(layers)
            )
            self.classifier = nn.Conv1d(hidden, classes, kernel_size=1)

        def forward(self, x):
            z = self.input_projection(x)
            for layer in self.layers:
                z = layer(z)
            return self.classifier(z)

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_norm = nn.LayerNorm(in_dim)
            self.input_projection = nn.Linear(in_dim, hidden)
            self.bilstm = nn.LSTM(
                input_size=hidden,
                hidden_size=hidden,
                num_layers=2,
                batch_first=True,
                bidirectional=True,
                dropout=0.15,
            )
            self.lstm_projection = nn.Conv1d(hidden * 2, hidden, kernel_size=1)
            self.first_stage = SingleStageTCN(hidden, class_count, hidden, 6, 0.15)
            self.refine_stages = nn.ModuleList(
                SingleStageTCN(class_count, class_count, hidden, 6, 0.15)
                for _ in range(2)
            )

        def forward(self, x):
            z = torch.relu(self.input_projection(self.input_norm(x)))
            z, _ = self.bilstm(z)
            z = self.lstm_projection(z.transpose(1, 2))
            logits = self.first_stage(z)
            for stage in self.refine_stages:
                logits = stage(torch.softmax(logits, dim=1))
            return logits

    return Model()


def _make_asformer(in_dim: int, class_count: int, hidden: int = 64, heads: int = 4):
    """构建 ASFormer 风格网络（局部卷积 + 多头自注意力 + FFN，带正弦位置编码）。"""
    import math
    import torch
    import torch.nn as nn

    def sinusoidal_position(length: int, dim: int, device):
        pos = torch.arange(length, device=device).float().unsqueeze(1)
        idx = torch.arange(dim, device=device).float().unsqueeze(0)
        div = torch.exp(torch.floor(idx / 2) * (-math.log(10000.0) / max(dim, 1)))
        enc = pos * div
        out = torch.zeros(length, dim, device=device)
        out[:, 0::2] = torch.sin(enc[:, 0::2])
        out[:, 1::2] = torch.cos(enc[:, 1::2])
        return out

    class Block(nn.Module):
        def __init__(self, dilation: int):
            super().__init__()
            self.local = nn.Conv1d(hidden, hidden, kernel_size=3, padding=dilation, dilation=dilation)
            self.local_norm = nn.LayerNorm(hidden)
            self.attn = nn.MultiheadAttention(hidden, heads, dropout=0.15, batch_first=True)
            self.attn_norm = nn.LayerNorm(hidden)
            self.ffn = nn.Sequential(
                nn.Linear(hidden, hidden * 4),
                nn.GELU(),
                nn.Dropout(0.15),
                nn.Linear(hidden * 4, hidden),
            )
            self.ffn_norm = nn.LayerNorm(hidden)
            self.dropout = nn.Dropout(0.15)

        def forward(self, x):
            local = self.local(x.transpose(1, 2)).transpose(1, 2)
            x = self.local_norm(x + self.dropout(torch.relu(local)))
            attn, _ = self.attn(x, x, x, need_weights=False)
            x = self.attn_norm(x + self.dropout(attn))
            return self.ffn_norm(x + self.dropout(self.ffn(x)))

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_norm = nn.LayerNorm(in_dim)
            self.projection = nn.Linear(in_dim, hidden)
            self.blocks = nn.ModuleList([Block(2 ** (i % 4)) for i in range(4)])
            self.classifier = nn.Sequential(
                nn.LayerNorm(hidden),
                nn.Linear(hidden, class_count),
            )

        def forward(self, x):
            _, time, _ = x.shape
            z = self.projection(self.input_norm(x))
            z = z + sinusoidal_position(time, z.shape[-1], x.device).unsqueeze(0)
            for block in self.blocks:
                z = block(z)
            return self.classifier(z).transpose(1, 2)

    return Model()


def _make_bigru(in_dim: int, class_count: int, hidden: int = 64):
    """构建 BiGRU 网络（3 层双向 GRU → 时序卷积头）。"""
    import torch
    import torch.nn as nn

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_norm = nn.LayerNorm(in_dim)
            self.projection = nn.Linear(in_dim, hidden)
            self.gru = nn.GRU(hidden, hidden, num_layers=3, batch_first=True, bidirectional=True, dropout=0.15)
            self.temporal_head = nn.Sequential(
                nn.Conv1d(hidden * 2, hidden, kernel_size=3, padding=1),
                nn.ReLU(),
                nn.Dropout(0.15),
                nn.Conv1d(hidden, class_count, kernel_size=1),
            )

        def forward(self, x):
            z = torch.relu(self.projection(self.input_norm(x)))
            z, _ = self.gru(z)
            return self.temporal_head(z.transpose(1, 2))

    return Model()


class CleanMSTCNBiLSTMSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 MS-TCN + BiLSTM 离线模型。

    当前 best checkpoint 对应基础 v2 特征：
        clean_bbox_v2_top1_impute，113 维。
    """

    model_version = "clean_mstcn_bilstm_v1"
    feature_method = "v2"

    def _build_model(self, in_dim: int, class_count: int):
        return _make_mstcn_bilstm(in_dim, class_count)


class CleanASFormerSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 ASFormer 风格离线模型。

    当前 best checkpoint 对应 v2 + business_priors：
        clean_bbox_v2_top1_impute+business_priors，121 维。
    """

    model_version = "clean_asformer_v1"
    feature_method = "business_priors"

    def preprocess(self, frames: Sequence[FrameDetection]) -> ModelInput:
        return add_business_priors(super().preprocess(frames))

    def _build_model(self, in_dim: int, class_count: int):
        return _make_asformer(in_dim, class_count)


class CleanBiGRUSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 BiGRU 离线模型。

    当前 best checkpoint 对应 v2 + center window + business_priors：
        clean_bbox_v2_top1_impute+center_window+business_priors，249 维。
    """

    model_version = "clean_bigru_v1"
    feature_method = "window_stats+business_priors"

    def preprocess(self, frames: Sequence[FrameDetection]) -> ModelInput:
        return add_business_priors(add_centered_window_stats(super().preprocess(frames)))

    def _build_model(self, in_dim: int, class_count: int):
        return _make_bigru(in_dim, class_count)


# ==================== 因果滑窗 GRU（训练框架 sliding_window_temporal 产物） ====================

NODEP_GRU_LABELS = (
    "idle",
    "water_injection",
    "flush",
    "long_brush_insert",
    "long_brush_withdraw",
    "short_brush_cleaning",
)
_WINDOW_BATCH = 1024


def _make_window_gru(input_dim: int, class_count: int, hidden: int, num_layers: int, dropout: float):
    """单向多层 GRU + 线性头，输入 `[B, window, F]`，取窗口末帧输出 `[B, C]`（state_dict 键 rnn.* / head.*）。"""
    import torch.nn as nn

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.rnn = nn.GRU(input_dim, hidden, num_layers=num_layers, batch_first=True, dropout=dropout)
            self.head = nn.Linear(hidden, class_count)

        def forward(self, x):
            out, _ = self.rnn(x)
            return self.head(out[:, -1])

    return Model()


def _causal_windows(x: np.ndarray, window: int) -> np.ndarray:
    """`[T,F]` → 每帧一个以它为末帧的窗口 `[T,window,F]`；开头不足一窗的用首帧重复补齐。"""
    padded = np.concatenate([np.repeat(x[:1], window - 1, axis=0), x], axis=0)
    return np.lib.stride_tricks.sliding_window_view(padded, window, axis=0).transpose(0, 2, 1)


def _check_window_gru_meta(meta: Dict[str, Any], class_count: int) -> None:
    """校验训练框架 meta 与本策略的特征 / 类别 / 切窗契约一致，不一致即抛。"""
    schema = meta.get("feature_schema") or {}
    if (schema.get("version"), schema.get("dim")) != (NODEP_FEATURE_VERSION, NODEP_FEATURE_DIM):
        raise ValueError(
            f"meta 特征契约 {schema.get('version')}/{schema.get('dim')} "
            f"!= {NODEP_FEATURE_VERSION}/{NODEP_FEATURE_DIM}"
        )
    model_cfg = meta.get("model") or {}
    if (model_cfg.get("type"), model_cfg.get("input_dim"), model_cfg.get("num_classes")) != (
        "gru", NODEP_FEATURE_DIM, class_count,
    ):
        raise ValueError(f"meta model 段与本策略不符: {model_cfg}（期望 gru/{NODEP_FEATURE_DIM}/{class_count}）")
    if meta.get("pipeline") != "sliding_window_temporal" or int(meta.get("window") or 0) < 1:
        raise ValueError(f"meta 非滑窗时序产物: pipeline={meta.get('pipeline')} window={meta.get('window')}")


def pack_window_gru_checkpoint(src: Path, dst: Path) -> None:
    """训练框架交付（`src` + `<src>.meta.json`）→ 自包含部署物料 `dst = {model_state, meta}`。

    sha256 绑定与契约在此校验；丢弃 optimizer 等训练态。
    """
    import torch

    src, dst = Path(src), Path(dst)
    meta_path = Path(f"{src}.meta.json")
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    if (meta.get("checkpoint_binding") or {}).get("sha256") != hashlib.sha256(src.read_bytes()).hexdigest():
        raise ValueError(f"权重 sha256 与 {meta_path.name} checkpoint_binding 不一致: {src}")
    _check_window_gru_meta(meta, len(NODEP_GRU_LABELS))
    checkpoint = torch.load(src, map_location="cpu", weights_only=True)
    torch.save({"model_state": checkpoint["model_state"], "meta": meta}, dst)


class CleanNodepGRUSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段因果滑窗 GRU 离线模型（特征 ama-v3-concat23-nodep-226d，6 类）。

    `model_path` 须是 `pack_window_gru_checkpoint` 打出的自包含物料：加载时校验内嵌 meta 的特征契约 / 类别数，
    按其 `model` 段重建网络、按 `window` 切窗，缺失或不符即抛。
    `model_input_fps` 须等于训练帧率、`confidence_override` 须与训练标注口径一致——配错不报错、
    结果静默变差。
    """

    model_version = "clean_gru_nodep226d"
    feature_method = "nodep_concat"
    labels = NODEP_GRU_LABELS

    def __init__(
        self,
        model_path: str | None = None,
        *,
        model_input_fps: float,
        min_duration_s: float = 0.2,
        confidence_override: float | None = None,
        frame_width: int = 640,
        frame_height: int = 480,
    ):
        if float(model_input_fps) <= 0:
            raise ValueError(f"model_input_fps 必须大于 0: {model_input_fps}")
        if confidence_override is not None and not 0.0 <= float(confidence_override) <= 1.0:
            raise ValueError(f"confidence_override 必须在 0..1: {confidence_override}")
        super().__init__(
            model_path=model_path,
            min_duration_s=min_duration_s,
            fps=model_input_fps,
            frame_width=frame_width,
            frame_height=frame_height,
        )
        self.confidence_override = None if confidence_override is None else float(confidence_override)
        self._window = 0

    def preprocess(self, frames: Sequence[FrameDetection]) -> ModelInput:
        """按 ts 降采样到 model_input_fps（只挑真实帧，ts 与 detections.jsonl 位级相等）→ 226 维特征。"""
        return build_nodep_concat_features(
            resample_by_ts(frames, self.fps),
            self.fps,
            self.frame_width,
            self.frame_height,
            self.confidence_override,
        )

    def _predict_with_model(self, model_input: ModelInput) -> np.ndarray:
        """每帧取以它为末帧的窗口前向，返回逐帧 softmax `[T, len(self.labels)]`。"""
        import torch

        if self._model is None:
            self._load_model(model_input, len(self.labels))
        windows = _causal_windows(np.asarray(model_input.features, dtype=np.float32), self._window)
        self._model.eval()
        chunks = []
        with torch.no_grad():
            for start in range(0, len(windows), _WINDOW_BATCH):
                batch = torch.from_numpy(np.ascontiguousarray(windows[start:start + _WINDOW_BATCH]))
                chunks.append(torch.softmax(self._model(batch), dim=-1).numpy())
        return np.concatenate(chunks, axis=0).astype(np.float32)

    def _load_model(self, model_input: ModelInput, class_count: int) -> None:
        """读内嵌 meta 并校验 → 按 meta 重建 GRU → strict 加载 `model_state`。"""
        import torch

        path = Path(str(self.model_path))
        if not path.exists():
            raise FileNotFoundError(f"clean 离线模型物料不存在: {path}")
        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        meta = checkpoint.get("meta")
        if meta is None:
            raise ValueError(f"物料缺内嵌 meta（训练框架原始 checkpoint 须先 pack_window_gru_checkpoint）: {path}")
        _check_window_gru_meta(meta, class_count)

        cfg = meta["model"]
        model = _make_window_gru(
            NODEP_FEATURE_DIM, class_count, int(cfg["hidden"]), int(cfg["num_layers"]), float(cfg.get("dropout", 0.0)),
        )
        model.load_state_dict(checkpoint["model_state"], strict=True)
        self._window = int(meta["window"])
        self._model = model
