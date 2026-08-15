"""CLEAN bbox 特征块：检测框序列 → v3 固定维（71）时序特征。

与 offline-model 仓的 `clean_bbox_v3_detectable` 对齐：
    - hand 使用 top-2 槽位；
    - 其它目标使用 top-1，不做同类多框加权平均；
    - 每个目标包含 present/conf/cx/cy/area/speed/missing_age/imputed；
    - 对关键目标对补 valid/dist/delta；
    - 最后补时间位置编码。

全部是无状态纯函数。窗口统计、业务先验等**模型专属**增强不在这里，在
`impl/clean.py` 里由各 Segmenter 自己叠加——本块只出所有模型共享的那 71 维。
"""

from __future__ import annotations

import math
from typing import Dict, List, Sequence, Tuple

import numpy as np

from app.domain.detection import Detection, FrameFeature
from app.services.inference.offline.models import FeatureBlock, finite

FEATURE_VERSION = "clean_bbox_v3_detectable"

# 特征工程的三个兜底常量。都只是**兜底**：真实采样率由 `effective_fps` 从帧 ts 反推，
# 分辨率优先取 `FrameFeature.frame_*`（pool 盖章、store 回读还原），仅当这些缺失时才落到这里。
DEFAULT_FPS = 7.5
DEFAULT_FRAME_WIDTH = 640
DEFAULT_FRAME_HEIGHT = 480

# 只保留**部署中的检测器真会产出**的类别，与两个 checkpoint 的 `names` 严格对齐：
#   clean-large-best → hand / scope_control_body / scope_mid_section
#   clean-small-best → syringe / air_gun / scope_distal_end
#
# 刷具（short_brush / long_brush / brush_tip_out）**刻意不在此列**：现场实测这类细长、
# 高度遮挡的目标基本检不出，且按 CLEAN 模型提案 §3.2D 已定为不作为输入检测类别——
# 「长刷/短刷」保留业务语义，但证据来自手部局部画面、可见大目标与跨帧变化，不来自刷具框。
# 曾经把它们列在这里的后果是 33 列特征恒为零，白占输入维度并让 normalizer 的 std=0。
OBJECTS = [
    "hand",
    "syringe",
    "air_gun",
    "scope_control_body",
    "scope_mid_section",
    "scope_distal_end",
]

OBJECT_ALIASES = {name: name for name in OBJECTS}

# 目标对关系特征（valid/dist/delta）。同样只保留两端都真能检出的对——涉及刷具的 5 组
# 已随上面一并移除。
PAIR_FEATURES = [
    ("air_gun", "scope_distal_end"),
    ("syringe", "scope_distal_end"),
]

# 每帧每类保留的候选框上限。槽位最多用到 top-2（hand），排序后多出来的部分对结果无影响，
# 但 `*_candidate_count` 统计的是**全部**候选，故截断只发生在排序之后（见 _select_*）。
_BOX_WIDTH = 5  # [present, cx, cy, area, conf]


def build(
    frames: Sequence[FrameFeature],
    frame_width: int = DEFAULT_FRAME_WIDTH,
    frame_height: int = DEFAULT_FRAME_HEIGHT,
    fallback_fps: float = DEFAULT_FPS,
) -> FeatureBlock:
    """帧级 FrameFeature 序列 → 71 维 bbox 特征块。

    每帧 `FrameFeature.by_source` 里的多流检测在此按帧合并消费（无需上游先融合）。
    """
    frame_width = max(1, int(frame_width))
    frame_height = max(1, int(frame_height))
    ts = [ff.ts for ff in frames]  # FrameFeature.ts 已在 store.load 边界统一 float
    frame_count = len(frames)
    if frame_count <= 0:
        names = base_feature_names()
        return FeatureBlock(
            values=np.zeros((0, len(names)), dtype=np.float32),
            names=names,
            ts=[],
            version=FEATURE_VERSION,
        )

    fps = effective_fps(ts, float(fallback_fps))
    boxes = _collect_object_boxes(frames, frame_width, frame_height)
    values, names = _build_feature_matrix(boxes, frame_count, fps)
    return FeatureBlock(
        values=values,
        names=names,
        ts=ts,
        version=FEATURE_VERSION,
        spans={"bbox": [0, len(names)]},
    )


def base_feature_names() -> List[str]:
    """基础 v3 的 71 个特征列名（跑一遍空矩阵取名，避免维护第二份清单）。"""
    return _build_feature_matrix({name: [[]] for name in OBJECTS}, 1, DEFAULT_FPS)[1]


def effective_fps(timestamps: Sequence[float], fallback_fps: float = DEFAULT_FPS) -> float:
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


# ==================== 逐帧候选框归拢 ====================


def _collect_object_boxes(
    frames: Sequence[FrameFeature], frame_width: int, frame_height: int
) -> Dict[str, List[List[np.ndarray]]]:
    """把检测框按 {目标类别: [每帧一个候选框列表]} 归拢。

    每帧遍历 `FrameFeature.by_source` 各流的检测（多流按帧合并），每个框存成一条
    5 元 `[present, cx, cy, area, conf]`。

    **这里曾是 O(T × 检测框数) 的源头**：旧实现给每个检测框都分配一条全长 `[T, 5]`
    稀疏数组、只写一行，一条 step 的 hand 有 2661 个框就是 2661 条 `[1886,5]`（约
    100 MB），下游还要逐帧全扫这 2661 条。改成按帧装桶后，槽位选择只看本帧那几个候选，
    内存与耗时都退回 O(检测框数)。候选在桶内的先后顺序与旧实现的遍历顺序一致，故
    同分候选的稳定排序结果不变（N6 逐值相等的前提）。
    """
    frame_count = len(frames)
    out: Dict[str, List[List[np.ndarray]]] = {
        name: [[] for _ in range(frame_count)] for name in OBJECTS
    }
    for idx, ff in enumerate(frames):
        # 帧级分辨率优先（pool 盖章、store 回读还原）；缺失回退传入默认。同帧各流同值。
        width = max(1, int(ff.frame_width or frame_width))
        height = max(1, int(ff.frame_height or frame_height))
        for fd in ff.by_source.values():
            for det in fd.detections:
                obj = OBJECT_ALIASES.get(str(det.class_name))
                if obj is None:
                    continue
                cx, cy, area = _bbox_to_center_area(det, width, height)
                out[obj][idx].append(
                    np.array(
                        [
                            1.0,
                            float(cx),
                            float(cy),
                            float(area),
                            max(0.0, min(1.0, float(det.confidence))),
                        ],
                        dtype=np.float32,
                    )
                )
    return out


def _bbox_to_center_area(det: Detection, width: int, height: int) -> Tuple[float, float, float]:
    """xyxy 框空间归一化后返回 (中心 cx, 中心 cy, 面积)，坐标/面积均截到 [0,1]。"""
    if len(det.bbox) < 4:
        return 0.0, 0.0, 0.0
    x1, y1, x2, y2 = [float(v) for v in det.bbox[:4]]

    # FeatureStore 当前保存的是 xyxy。若数值已经在 0-1，则按归一化坐标处理；
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


# ==================== 槽位选择与插值 ====================


def _box_score(row: np.ndarray, prev_center: np.ndarray | None = None) -> float:
    """槽位竞争打分：conf×√area，再对偏离上一帧中心的位移做惩罚；缺框记 -1。"""
    present, cx, cy, area, conf = [float(x) for x in row]
    if present <= 0:
        return -1.0
    score = conf * math.sqrt(max(area, 1e-6))
    if prev_center is not None:
        score -= 0.15 * min(math.dist((cx, cy), tuple(prev_center)), math.sqrt(2.0))
    return score


def _frame_buckets(
    boxes: Dict[str, List[List[np.ndarray]]], obj: str, frames: int
) -> List[List[np.ndarray]]:
    """取某目标的逐帧候选桶；缺该目标时返回等长空桶。"""
    got = boxes.get(obj)
    if not got:
        return [[] for _ in range(frames)]
    return got


def _select_hand_slots(
    buckets: List[List[np.ndarray]], frames: int
) -> Tuple[np.ndarray, List[np.ndarray]]:
    """每帧按打分取 hand 的 top-2 槽位（双手），并返回逐帧 hand 计数。"""
    hand_count = np.zeros(frames, dtype=np.float32)
    slots = [
        np.zeros((frames, _BOX_WIDTH), dtype=np.float32),
        np.zeros((frames, _BOX_WIDTH), dtype=np.float32),
    ]
    for t in range(frames):
        candidates = list(buckets[t])
        hand_count[t] = len(candidates)
        candidates.sort(key=lambda row: _box_score(row), reverse=True)
        for slot_idx, row in enumerate(candidates[:2]):
            slots[slot_idx][t] = row
    return hand_count, slots


def _select_top1_slot(
    buckets: List[List[np.ndarray]], frames: int
) -> Tuple[np.ndarray, np.ndarray]:
    """每帧取单目标 top-1 槽位（带上一帧中心做时序连续性打分），返回计数与槽位。"""
    count = np.zeros(frames, dtype=np.float32)
    slot = np.zeros((frames, _BOX_WIDTH), dtype=np.float32)
    prev_center: np.ndarray | None = None
    for t in range(frames):
        candidates = list(buckets[t])
        count[t] = len(candidates)
        if not candidates:
            continue
        candidates.sort(key=lambda row: _box_score(row, prev_center), reverse=True)
        slot[t] = candidates[0]
        prev_center = slot[t, 1:3]
    return count, slot


def _missing_age(raw_present: np.ndarray, max_gap: int) -> np.ndarray:
    """连续缺失帧数归一化到 [0,1]（越久没检到越接近 1），一旦命中清零。"""
    out = np.zeros(len(raw_present), dtype=np.float32)
    age = 0
    for idx, flag in enumerate(raw_present > 0):
        age = 0 if flag else age + 1
        out[idx] = min(age, max_gap) / max(1, max_gap)
    return out


def _impute_short_gaps(
    raw: np.ndarray, fps: float, max_gap: int = 6
) -> Tuple[np.ndarray, np.ndarray]:
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


# ==================== 矩阵拼装 ====================


def _build_feature_matrix(
    object_boxes: Dict[str, List[List[np.ndarray]]],
    frames: int,
    fps: float,
) -> Tuple[np.ndarray, List[str]]:
    """拼装完整 v3 矩阵：hand top-2 + 各目标 top-1 + 关键目标对 + 时间编码，返回 (矩阵, 列名)。"""
    blocks: List[np.ndarray] = []
    names: List[str] = []
    centers: Dict[str, np.ndarray] = {}
    active: Dict[str, np.ndarray] = {}

    hand_count, hand_slots = _select_hand_slots(_frame_buckets(object_boxes, "hand", frames), frames)
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
        count, slot = _select_top1_slot(_frame_buckets(object_boxes, obj, frames), frames)
        feature, obj_active = _impute_short_gaps(slot, fps)
        blocks.append(
            np.concatenate([(np.clip(count, 0, 3) / 3.0)[:, None], feature], axis=1).astype(np.float32)
        )
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
    return finite(np.concatenate(blocks, axis=1)), names
