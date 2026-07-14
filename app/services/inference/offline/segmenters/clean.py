"""CLEAN stage 离线动作分割 baseline —— 规则分类器（自包含单文件）。

一策略一模块，内聚三段（对齐 workflows/mock.py「一个文件装齐」的风格）：
    1. FeatureVectorizer/ModelInput —— clean 专属 62 维特征转换（私有，不对外当共享层）；
    2. 规则薄分类器 —— 逐帧动作判断（air_injection/flush/short_brush_cleaning/long_brush_insert/idle）；
    3. CleanSegmenter(OfflineSegmenter) —— preprocess 拥有特征转换、segment 归并成 SegmentFact。

数据流（不经任何自定义中间数据壳）：
    Mapping[source, List[FrameDetections]]  ← Runner.load_many
        │  preprocess: 按 ts 跨 source 拍平成有序 List[FrameDetections]
        ▼  FeatureVectorizer → ModelInput([T, 62])
        │  segment: 分类器逐帧标签 → 归并连续同标签帧
        ▼  List[SegmentFact]  → Runner.replace_segments

注意：这是运行时规则 baseline，不含训练逻辑，不代表最终精度。接入真实时序模型（MS-TCN/
ASFormer 等）时新增一个同样自包含的单文件策略、YAML `offline.class` 切过去即可。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Dict, List, Mapping, Sequence, Tuple

from app.domain.detection import Detection, FrameDetections
from app.services.inference.models import SegmentFact
from app.services.inference.offline.segmenter import OfflineSegmenter


# ==================== clean 专属特征定义（私有） ====================

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

# 训练类别名 → 特征目标名（当前恒等；类别名改动时只改这里）
OBJECT_ALIASES = {name: name for name in OBJECTS}

MODEL_VERSION = "clean_rule_v1"


@dataclass(frozen=True)
class ModelInput:
    """clean 私有的定维时序特征张量（不依赖 numpy/torch，便于纯 CPU 环境直接跑）。"""

    features: List[List[float]]
    feature_names: List[str]
    timestamps: List[float]
    fps: float

    @property
    def frame_count(self) -> int:
        return len(self.features)

    @property
    def feature_dim(self) -> int:
        return len(self.feature_names)


@dataclass
class _ObjectStats:
    count: float = 0.0
    cx: float = 0.0
    cy: float = 0.0
    area: float = 0.0

    @property
    def present(self) -> bool:
        return self.count > 0.0


class FeatureVectorizer:
    """把按 ts 拍平的 FrameDetections 序列转成 [T, 62] 定维时序特征。

    列顺序：9 类目标 * (count/cx/cy/area/speed) = 45 + 7 组关系 * (valid/dist) = 14 + 时间位置 3 = 62。
    """

    def __init__(self, frame_width: int = 640, frame_height: int = 480):
        self.frame_width = max(1, int(frame_width))
        self.frame_height = max(1, int(frame_height))

    def transform(self, frames: Sequence[FrameDetections], fps: float) -> ModelInput:
        stats_by_frame = [self._collect_frame_stats(f.detections) for f in frames]
        timestamps = [float(f.timestamp) for f in frames]
        feature_names = self.feature_names()
        rows: List[List[float]] = []
        last_centers: Dict[str, Tuple[float, float] | None] = {name: None for name in OBJECTS}
        frame_count = max(1, len(stats_by_frame))
        safe_fps = max(float(fps), 1e-6)

        for idx, stats in enumerate(stats_by_frame):
            row: List[float] = []
            centers: Dict[str, Tuple[float, float]] = {}
            present: Dict[str, bool] = {}

            for obj in OBJECTS:
                item = stats.get(obj, _ObjectStats())
                center = (item.cx, item.cy)
                previous = last_centers.get(obj)
                speed = 0.0
                if item.present and previous is not None:
                    speed = min(math.dist(center, previous) * safe_fps, 5.0) / 5.0
                if item.present:
                    last_centers[obj] = center

                row.extend([min(item.count, 3.0) / 3.0, item.cx, item.cy, item.area, speed])
                centers[obj] = center
                present[obj] = item.present

            for left, right in PAIR_FEATURES:
                valid = 1.0 if present[left] and present[right] else 0.0
                dist = 0.0
                if valid:
                    dist = min(math.dist(centers[left], centers[right]), math.sqrt(2.0)) / math.sqrt(2.0)
                row.extend([valid, dist])

            t_norm = 0.0 if frame_count <= 1 else idx / (frame_count - 1)
            row.extend([t_norm, math.sin(2 * math.pi * t_norm), math.cos(2 * math.pi * t_norm)])
            rows.append(row)

        return ModelInput(features=rows, feature_names=feature_names, timestamps=timestamps, fps=float(fps))

    @staticmethod
    def feature_names() -> List[str]:
        names: List[str] = []
        for obj in OBJECTS:
            names.extend([f"{obj}_count", f"{obj}_cx", f"{obj}_cy", f"{obj}_area", f"{obj}_speed"])
        for left, right in PAIR_FEATURES:
            names.extend([f"{left}_to_{right}_valid", f"{left}_to_{right}_dist"])
        names.extend(["t_norm", "t_sin", "t_cos"])
        return names

    def _collect_frame_stats(self, detections: Sequence[Detection]) -> Dict[str, _ObjectStats]:
        """聚合单帧检测框：同类多框按面积加权中心 + 平均面积；count 保留目标数。"""
        buckets: Dict[str, List[Tuple[float, float, float]]] = {name: [] for name in OBJECTS}
        for det in detections:
            obj = OBJECT_ALIASES.get(str(det.class_name))
            if obj is None:
                continue
            cx, cy, area = self._bbox_to_center_area(det)
            buckets[obj].append((cx, cy, area))

        stats: Dict[str, _ObjectStats] = {}
        for obj, entries in buckets.items():
            if not entries:
                continue
            count = float(len(entries))
            weight_sum = sum(max(area, 1e-6) for _, _, area in entries)
            cx = sum(cx * max(area, 1e-6) for cx, _, area in entries) / weight_sum
            cy = sum(cy * max(area, 1e-6) for _, cy, area in entries) / weight_sum
            area = sum(area for _, _, area in entries) / count
            stats[obj] = _ObjectStats(count=count, cx=cx, cy=cy, area=area)
        return stats

    def _bbox_to_center_area(self, det: Detection) -> Tuple[float, float, float]:
        """Detection.bbox 为像素 xyxy → 0-1 归一化中心与面积。"""
        if len(det.bbox) < 4:
            return 0.0, 0.0, 0.0
        x1, y1, x2, y2 = [float(v) for v in det.bbox[:4]]
        width = max(0.0, x2 - x1)
        height = max(0.0, y2 - y1)
        cx = (x1 + x2) * 0.5 / self.frame_width
        cy = (y1 + y2) * 0.5 / self.frame_height
        area = (width / self.frame_width) * (height / self.frame_height)
        return min(max(cx, 0.0), 1.0), min(max(cy, 0.0), 1.0), min(max(area, 0.0), 1.0)


# ==================== 离线分割策略 ====================

class CleanSegmenter(OfflineSegmenter):
    """CLEAN stage 规则 baseline：62 维特征 + 逐帧规则分类 → 动作分段。

    Args:
        name: 策略身份（= SegmentFact.source），工厂注入。
        subscribes: 订阅的 detector name 列表（如 clean_large/clean_small），工厂注入。
        min_duration_s: 一段的最短时长，短于此丢弃（默认 0.2s）。
        fps: 特征序列采样帧率（用于速度特征），默认 7.5。
        frame_width/frame_height: bbox 归一化用画面尺寸，默认 640x480。
    """

    def __init__(
        self,
        name: str,
        subscribes: Sequence[str],
        min_duration_s: float = 0.2,
        fps: float = 7.5,
        frame_width: int = 640,
        frame_height: int = 480,
    ):
        super().__init__(name, subscribes)
        self.min_duration_s = max(0.0, float(min_duration_s))
        self.fps = float(fps)
        self.vectorizer = FeatureVectorizer(frame_width, frame_height)
        self._last_result: dict | None = None

    def preprocess(self, streams: Mapping[str, Sequence[FrameDetections]]) -> ModelInput:
        """按 ts 跨 source 拍平成有序 List[FrameDetections] → 62 维 ModelInput。"""
        by_ts: Dict[float, List[Detection]] = {}
        for src in self.subscribes:
            for fd in streams.get(src, ()):
                by_ts.setdefault(float(fd.timestamp), []).extend(fd.detections)

        frames = [
            FrameDetections(detections=by_ts[ts], metadata={}, timestamp=ts)
            for ts in sorted(by_ts)
        ]
        return self.vectorizer.transform(frames, self.fps)

    def segment(self, model_input: ModelInput) -> List[SegmentFact]:
        mi = model_input
        name_to_idx = {name: idx for idx, name in enumerate(mi.feature_names)}

        preds: List[Tuple[float, str, float]] = []
        for row, ts in zip(mi.features, mi.timestamps):
            label, conf, _scores = self._predict_row(row, name_to_idx)
            preds.append((float(ts), label, float(conf)))

        segments: List[SegmentFact] = []
        cur_label: str | None = None
        cur_start = cur_end = 0.0
        cur_conf = 0.0
        cur_count = 0

        def flush() -> None:
            nonlocal cur_label
            if cur_label is not None and (cur_end - cur_start) >= self.min_duration_s:
                segments.append(SegmentFact(
                    source=self.name,
                    label=cur_label,
                    start=round(cur_start, 6),
                    end=round(cur_end, 6),
                    conf=min(1.0, max(0.0, cur_conf / max(cur_count, 1))),
                    meta={"model_version": MODEL_VERSION},
                ))
            cur_label = None

        for ts, label, conf in preds:
            if label == "idle":
                flush()
                continue
            if cur_label != label:
                flush()
                cur_label = label
                cur_start = cur_end = ts
                cur_conf = conf
                cur_count = 1
            else:
                cur_end = ts
                cur_conf += conf
                cur_count += 1
        flush()

        self._last_result = {
            "model_version": MODEL_VERSION,
            "frame_count": mi.frame_count,
            "frame_predictions": [
                {"ts": ts, "label": label, "conf": round(conf, 5)} for ts, label, conf in preds
            ],
            "segments": [s.to_json() for s in segments],
        }
        return segments

    def debug_result(self) -> dict | None:
        return self._last_result

    # ---- 逐帧规则分类 ----

    @staticmethod
    def _value(row: List[float], name_to_idx: Dict[str, int], name: str) -> float:
        idx = name_to_idx.get(name)
        if idx is None or idx >= len(row):
            return 0.0
        return float(row[idx])

    def _presence(self, row: List[float], name_to_idx: Dict[str, int], obj: str) -> float:
        """存在强度：count 特征存的是 count/3，这里转回 0-1。"""
        return min(1.0, self._value(row, name_to_idx, f"{obj}_count") * 3.0)

    def _predict_row(
        self, row: List[float], name_to_idx: Dict[str, int]
    ) -> Tuple[str, float, Dict[str, float]]:
        """一帧动作判断。器械动作优先 > 短刷 > 长刷 > idle；同帧多类共现更稳。"""
        hand = self._presence(row, name_to_idx, "hand")
        short_brush = self._presence(row, name_to_idx, "short_brush")
        long_brush = max(
            self._presence(row, name_to_idx, "long_brush"),
            self._presence(row, name_to_idx, "brush_tip_out"),
        )
        syringe = self._presence(row, name_to_idx, "syringe")
        air_gun = self._presence(row, name_to_idx, "air_gun")
        scope_control = self._presence(row, name_to_idx, "scope_control_body")
        scope_mid = self._presence(row, name_to_idx, "scope_mid_section")
        scope_distal = self._presence(row, name_to_idx, "scope_distal_end")

        hand_short_valid = self._value(row, name_to_idx, "hand_to_short_brush_valid")
        short_scope_valid = self._value(row, name_to_idx, "short_brush_to_scope_control_body_valid")
        long_scope_valid = self._value(row, name_to_idx, "long_brush_to_scope_mid_section_valid")

        short_context = max(hand, scope_control, scope_distal, hand_short_valid, short_scope_valid)
        long_context = max(hand, scope_mid, scope_distal, long_scope_valid)

        scores = {
            "idle": 0.05,
            "long_brush_insert": min(long_brush, long_context) if long_brush else 0.0,
            "long_brush_withdraw": 0.0,
            "short_brush_cleaning": min(short_brush, short_context) if short_brush else 0.0,
            "flush": syringe,
            "air_injection": air_gun,
        }

        for label in ("air_injection", "flush", "short_brush_cleaning", "long_brush_insert"):
            if scores[label] > 0.0:
                return label, max(0.2, min(1.0, scores[label])), scores
        return "idle", 1.0, scores
