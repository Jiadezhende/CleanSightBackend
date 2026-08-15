"""CLEAN stage 离线模型策略。

本文件保持“单策略文件自包含”：
    - clean 专属特征转换（模块级纯函数）；
    - 三种离线模型结构；
    - 模型输出到 SegmentFact 的解码逻辑。

输入:
    OfflineRunner 从 FeatureStore.load(task_id, step_id) 读取 List[FrameFeature]
    （帧级、多流已在 by_source 内对齐、按 ts 升序）。

输出:
    List[SegmentFact]，由 Runner 校验并幂等写入 FactLedger。

注意:
    这里不包含训练流程。训练仍在独立 offline-model 仓内完成，后端只负责加载
    已训练权重并执行离线推理。若未配置 model_path，CLEAN 模型会硬失败；
    本地回环测试应使用已有的 mock.BrushRulesSegmenter。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Sequence, Tuple

import numpy as np

from app.domain.detection import Detection, FrameFeature
from app.services.inference.models import SegmentFact
from app.services.inference.offline.segmenter import OfflineSegmenter


FEATURE_VERSION = "clean_bbox_v3_detectable"

# 特征工程的三个兜底常量（Segmenter 构造默认值与导出 recipe 共用一处，避免两份漂移）。
# 都只是**兜底**：真实采样率由 `_effective_fps` 从帧 ts 反推，分辨率优先取 `FrameFeature.frame_*`
# （pool 盖章、store 回读还原），仅当这些缺失时才落到这里。
_DEFAULT_FPS = 7.5
_DEFAULT_FRAME_WIDTH = 640
_DEFAULT_FRAME_HEIGHT = 480

ACTION_LABELS = [
    "idle",
    "long_brush_insert",
    "long_brush_withdraw",
    "short_brush_cleaning",
    "flush",
    "air_injection",
]

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


@dataclass(frozen=True)
class ModelInput:
    """clean 离线模型输入。

    features:
        [T, F] 数值特征矩阵。基础 v3 为 71 维；具体模型可在
        覆盖的 preprocess() 内扩展为 73/151 等模型专属输入。
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
# clean 检测框序列 -> v3 固定维时序特征，与 offline-model 的
# `clean_bbox_v3_detectable` 对齐：
#     - hand 使用 top-2 槽位；
#     - 其它目标使用 top-1，不做同类多框加权平均；
#     - 每个目标包含 present/conf/cx/cy/area/speed/missing_age/imputed；
#     - 对关键目标对补 valid/dist/delta；
#     - 最后补时间位置编码。
#
# 这些是无状态函数（原 FeatureVectorizer 类无跨调用状态，只是命名空间）。窗口统计、
# 业务先验等模型专属增强由各模型在覆盖的 preprocess() 内自行叠加。


def build_base_features(
    frames: Sequence[FrameFeature],
    fps: float,
    frame_width: int = 640,
    frame_height: int = 480,
) -> ModelInput:
    """把 clean 帧级 FrameFeature 序列转换成 v3 固定维（71）时序特征。

    每帧 `FrameFeature.by_source` 里的多流检测在此按帧合并消费（无需上游先融合）。
    """
    frame_width = max(1, int(frame_width))
    frame_height = max(1, int(frame_height))
    timestamps = [ff.ts for ff in frames]  # FrameFeature.ts 已在 store.load 边界统一 float
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
    """基础 v3 的 71 个特征列名（跑一遍空矩阵取名，避免维护第二份清单）。"""
    return _build_feature_matrix({name: [] for name in OBJECTS}, 1, 7.5)[1]


def _finite_matrix(values: np.ndarray) -> np.ndarray:
    """把 NaN/inf 兜底成 0，保持模型输入尺寸稳定且数值可送入 torch。"""
    return np.nan_to_num(values, nan=0.0, posinf=0.0, neginf=0.0).astype(np.float32)


def _collect_object_arrays(
    frames: Sequence[FrameFeature], frame_width: int, frame_height: int
) -> Dict[str, List[np.ndarray]]:
    """把每帧检测框按目标类别归拢成 {obj: [每检测框一个 [T,5] 稀疏数组]}。

    每帧遍历 `FrameFeature.by_source` 各流的检测（多流按帧合并，同 idx 落同一行）。
    """
    frame_count = len(frames)
    out: Dict[str, List[np.ndarray]] = {name: [] for name in OBJECTS}
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
                arr = np.zeros((frame_count, 5), dtype=np.float32)
                arr[idx] = (
                    1.0,
                    float(cx),
                    float(cy),
                    float(area),
                    max(0.0, min(1.0, float(det.confidence))),
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
    """拼装完整 v3 矩阵：hand top-2 + 各目标 top-1 + 关键目标对 + 时间编码，返回 (矩阵, 列名)。"""
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


# -------------------- 导出 recipe（供离线导出器 importlib 取用） --------------------
#
# 统一签名 `(frames, visual) -> ModelInput`（见 offline/export/models.py）。导出器与将来的
# 融合 Segmenter.preprocess **调同一批函数**，故「训练样例」与「线上特征转换」不可能漂移。
# 这些是薄封装：真正的特征工程仍在上方模块级纯函数里，不在此复制一行。


def export_r0(frames: Sequence[FrameFeature], visual=None) -> ModelInput:
    """R0：bbox-only 对照基线（v3，71 维）。

    `visual` 恒被忽略——本 recipe 不消费像素，签名统一只是为了让导出器一视同仁地调用。
    R0 存在的意义是**对照基准**：后续 R1/R2 的增益必须相对它度量，故它也必须走同一套
    导出管道产出，而不是让训练侧另算一份。

    Args:
        frames: 帧级 FrameFeature 序列（按 ts 升序，多流已在 by_source 内对齐）
        visual: 忽略（保持 recipe 统一签名）
    """
    return build_base_features(frames, _DEFAULT_FPS, _DEFAULT_FRAME_WIDTH, _DEFAULT_FRAME_HEIGHT)


def export_r1(frames: Sequence[FrameFeature], visual=None) -> ModelInput:
    """R1：R0 + 全帧 CNN 深层全局池化向量。回答「视觉信息到底有没有用」。

    **R1a / R1b 是同一个 recipe，只差 backbone**（`--backbone yolo` / `--backbone resnet18`）：
    前者特征域与本场景匹配但与 bbox 同源，其增量只是"检测头丢掉的信息"；后者是一条独立的
    视觉通道。两者差异正是 R1 要测的核心变量，故 backbone 是导出器的配置项而非 recipe 分叉。

    取不到像素的帧：视觉块置零，**语义由末列 `visual_valid` 承载**——零值本身不表达
    "画面里什么都没有"，模型据 mask 判断（不变式 F4）。

    特征列布局：`[基础 71 维 | visual_global_0..C-1 | visual_valid]`
    视觉向量保持 backbone 原始尺度不做归一化——归一化统计量属训练侧，落在这里会与训练仓的
    normalizer 形成两份真源。

    Args:
        frames: 帧级 FrameFeature 序列
        visual: 必需，且须带 `global_vec`；缺失即硬失败（不静默退化成 R0）
    """
    base = export_r0(frames)
    if visual is None or visual.global_vec is None:
        raise ValueError("export_r1 需要带 global_vec 的 VisualFrames，请指定 --backbone")

    x = np.asarray(base.features, dtype=np.float32)
    if x.shape[0] != visual.global_vec.shape[0]:
        raise ValueError(
            f"视觉特征与 bbox 特征帧数不一致: {visual.global_vec.shape[0]} vs {x.shape[0]}"
        )

    valid = (
        np.ones(x.shape[0], dtype=np.float32) if visual.valid is None
        else np.asarray(visual.valid, dtype=np.float32)
    )
    g = np.asarray(visual.global_vec, dtype=np.float32) * valid[:, None]

    names = list(base.feature_names)
    names += [f"visual_global_{i}" for i in range(g.shape[1])]
    names += ["visual_valid"]
    return _with_features(
        base,
        np.concatenate([x, g, valid[:, None]], axis=1),
        names,
        f"{base.feature_version}+visual_global@{visual.backbone}",
    )


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
    """recipe：按业务规则叠加动作先验（接近度×存在×运动等），ASFormer/BiGRU 用。

    只保留**两端目标都真能检出**的先验：灌注（syringe 稳定贴近先端）与注气（air_gun 同理）。
    原先另有 6 维长/短刷先验，全部建立在刷具检测框上——刷具检不出（见 OBJECTS 注释），
    那 6 维恒为零，是纯噪声维度。长/短刷动作改由手部局部画面与跨帧变化承担，不在此处造先验。
    """
    x = np.asarray(model_input.features, dtype=np.float32)
    names = list(model_input.feature_names)
    n = {name: idx for idx, name in enumerate(names)}

    hand = np.maximum(_col(x, n, "hand_top1_present"), _col(x, n, "hand_top2_present"))
    syringe = _col(x, n, "syringe_present")
    air_gun = _col(x, n, "air_gun_present")

    syringe_near = _near_score(_col(x, n, "syringe_to_scope_distal_end_dist"))
    air_near = _near_score(_col(x, n, "air_gun_to_scope_distal_end_dist"))

    syringe_stable = syringe * syringe_near * (1.0 - np.clip(_col(x, n, "syringe_speed"), 0.0, 1.0))
    air_stable = air_gun * air_near * (1.0 - np.clip(_col(x, n, "air_gun_speed"), 0.0, 1.0))

    priors = np.stack(
        [
            hand * syringe_stable,
            hand * air_stable,
        ],
        axis=1,
    ).astype(np.float32)
    prior_names = [
        "prior_flush_stable",
        "prior_air_stable",
    ]
    return _with_features(
        model_input,
        np.concatenate([x, priors], axis=1).astype(np.float32),
        names + prior_names,
        f"{model_input.feature_version}+business_priors",
    )


class _CleanTorchSegmenter(OfflineSegmenter):
    """clean 模型策略基类：torch 模型加载 + 推理 + SegmentFact 解码。

    特征工程是模块级纯函数：`preprocess` 调 build_base_features 得基础 v3（71 维）；
    需叠加模型专属 recipe 的子类**覆盖 preprocess**，用 `super().preprocess()` 取基础特征后
    再调模块级特征函数（add_business_priors / add_centered_window_stats）。
    """

    model_version = "clean_model_v1"
    feature_method = "v3"

    def __init__(
        self,
        name: str,
        subscribes: Sequence[str],
        model_path: str | None = None,
        min_duration_s: float = 0.2,
        fps: float = _DEFAULT_FPS,
        frame_width: int = _DEFAULT_FRAME_WIDTH,
        frame_height: int = _DEFAULT_FRAME_HEIGHT,
    ):
        super().__init__(name, subscribes)
        self.model_path = model_path
        self.min_duration_s = max(0.0, float(min_duration_s))
        self.fps = float(fps)
        self.frame_width = max(1, int(frame_width))
        self.frame_height = max(1, int(frame_height))
        self._model = None
        self._normalizer: Tuple[Any, Any] | None = None
        self._last_result: dict | None = None

    def preprocess(self, frames: Sequence[FrameFeature]) -> ModelInput:
        """帧级 FrameFeature 序列 → 基础 v3 特征（71 维）。

        多流按帧合并折进 build_base_features（`frames` 已按 ts 升序、各流在 by_source 内对齐）。
        需叠加模型专属 recipe 的子类覆盖本方法，用 `super().preprocess()` 取基础特征后再变换。
        """
        return build_base_features(frames, self.fps, self.frame_width, self.frame_height)

    def segment(self, model_input: ModelInput) -> List[SegmentFact]:
        """跑模型得到逐帧标签，解码成 SegmentFact；未配 model_path 硬失败，不做规则降级。"""
        if model_input.frame_count == 0:
            return []

        if not self.model_path:
            raise ValueError(
                f"{type(self).__name__} 未配置 model_path；CLEAN 离线模型不做规则降级，"
                "本地回环请使用 mock.BrushRulesSegmenter"
            )

        labels, confs = self._predict_with_model(model_input)

        segments = self._labels_to_segments(model_input.timestamps, labels, confs)
        self._last_result = {
            "model_version": self.model_version,
            "model_class": type(self).__name__,
            "feature_method": self.feature_method,
            "feature_version": model_input.feature_version,
            "feature_dim": model_input.feature_dim,
            "frame_count": model_input.frame_count,
            "frame_predictions": [
                {"ts": ts, "label": ACTION_LABELS[label], "conf": round(float(conf), 5)}
                for ts, label, conf in zip(model_input.timestamps, labels, confs)
            ],
            "segments": [s.to_json() for s in segments],
        }
        return segments

    def debug_result(self) -> dict | None:
        """返回最近一次 segment() 的逐帧预测/分段调试快照（未跑过为 None）。"""
        return self._last_result

    def _predict_with_model(self, model_input: ModelInput) -> Tuple[List[int], List[float]]:
        """惰性加载权重，归一化+finite 兜底后前向，返回逐帧 (argmax 标签, 置信度)。"""
        import numpy as np
        import torch

        if self._model is None:
            self._load_model(model_input, len(ACTION_LABELS))

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
        labels = probs.argmax(axis=1).astype("int64").tolist()
        confs = probs.max(axis=1).astype("float32").tolist()
        return labels, confs

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
    ) -> List[SegmentFact]:
        """把逐帧标签合并成连续动作段（跳过 idle、过滤短于 min_duration_s 的段）。"""
        segments: List[SegmentFact] = []
        cur_label: int | None = None
        cur_start = cur_end = 0.0
        cur_conf = 0.0
        cur_count = 0

        def flush() -> None:
            nonlocal cur_label, cur_conf, cur_count
            if cur_label is not None and cur_label != 0 and (cur_end - cur_start) >= self.min_duration_s:
                segments.append(SegmentFact(
                    source=self.name,
                    label=ACTION_LABELS[cur_label],
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

    对应基础 v3 特征：
        clean_bbox_v3_detectable，71 维。
    """

    model_version = "clean_mstcn_bilstm_v1"
    feature_method = "v3"

    def _build_model(self, in_dim: int, class_count: int):
        return _make_mstcn_bilstm(in_dim, class_count)


class CleanASFormerSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 ASFormer 风格离线模型。

    对应 v3 + business_priors：
        clean_bbox_v3_detectable+business_priors，73 维。
    """

    model_version = "clean_asformer_v1"
    feature_method = "business_priors"

    def preprocess(self, frames: Sequence[FrameFeature]) -> ModelInput:
        return add_business_priors(super().preprocess(frames))

    def _build_model(self, in_dim: int, class_count: int):
        return _make_asformer(in_dim, class_count)


class CleanBiGRUSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 BiGRU 离线模型。

    对应 v3 + center window + business_priors：
        clean_bbox_v3_detectable+center_window+business_priors，151 维。
    """

    model_version = "clean_bigru_v1"
    feature_method = "window_stats+business_priors"

    def preprocess(self, frames: Sequence[FrameFeature]) -> ModelInput:
        return add_business_priors(add_centered_window_stats(super().preprocess(frames)))

    def _build_model(self, in_dim: int, class_count: int):
        return _make_bigru(in_dim, class_count)


# 兼容旧文档/旧测试中使用的 CleanSegmenter 名称；默认指向推荐离线 baseline。
CleanSegmenter = CleanMSTCNBiLSTMSegmenter
