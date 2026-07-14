"""离线模型运行时特征转换。

作用：
    将 worker 从 features.jsonl 读出的 OfflineFeatureSequence 转成模型可直接消费的
    固定维度时序输入。

输入：
    OfflineFeatureSequence:
        frames[t].detections_by_source[source] -> List[Detection]

输出：
    ModelInput:
        features: List[List[float]]，形状为 [time, feature_dim]
        feature_names: 与 features 每一列对应的名称
        timestamps: 每一帧对应的时间戳

说明：
    这里参考本地 offline baseline 的 62 维设计，但只保留推理链路需要的转换逻辑。
    不包含 Label Studio/ModelScope/YOLO 数据集转换、训练集划分、训练循环等训练侧代码。
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from app.domain.detection import Detection
from app.services.inference.offline.interfaces import OfflineFeatureSequence


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


@dataclass(frozen=True)
class ModelInput:
    """模型输入张量的轻量表示。

    不强制依赖 numpy/torch，便于本地 mock 环境直接运行；真实模型接入时可在模型内部
    转成 numpy.ndarray 或 torch.Tensor。
    """

    features: list[list[float]]
    feature_names: list[str]
    timestamps: list[float]
    fps: float

    @property
    def frame_count(self) -> int:
        return len(self.features)

    @property
    def feature_dim(self) -> int:
        return len(self.feature_names)


@dataclass
class _ObjectStats:
    """某一帧某一类目标的聚合结果。"""

    count: float = 0.0
    cx: float = 0.0
    cy: float = 0.0
    area: float = 0.0

    @property
    def present(self) -> bool:
        return self.count > 0.0


class FeatureVectorizer:
    """把 FeatureStore 检测序列转成固定维度时序特征。

    Args:
        frame_width: 后端检测 bbox 的画面宽度。当前流解码默认 640。
        frame_height: 后端检测 bbox 的画面高度。当前流解码默认 480。
    """

    def __init__(self, frame_width: int = 640, frame_height: int = 480):
        self.frame_width = max(1, int(frame_width))
        self.frame_height = max(1, int(frame_height))

    def transform(self, sequence: OfflineFeatureSequence) -> ModelInput:
        """执行完整特征转换。

        转换后的列顺序：
            1. 每类目标 count/cx/cy/area/speed，共 9 * 5 = 45 维；
            2. 目标关系 valid/dist，共 7 * 2 = 14 维；
            3. 时间位置 t_norm/t_sin/t_cos，共 3 维。
        """
        stats_by_frame = [self._collect_frame_stats(frame.detections) for frame in sequence.frames]
        timestamps = [frame.timestamp for frame in sequence.frames]
        feature_names = self.feature_names()
        rows: list[list[float]] = []
        last_centers: dict[str, tuple[float, float] | None] = {name: None for name in OBJECTS}
        frame_count = max(1, len(stats_by_frame))

        for idx, stats in enumerate(stats_by_frame):
            row: list[float] = []
            centers: dict[str, tuple[float, float]] = {}
            present: dict[str, bool] = {}

            for obj in OBJECTS:
                item = stats.get(obj, _ObjectStats())
                center = (item.cx, item.cy)
                previous = last_centers.get(obj)
                speed = 0.0
                if item.present and previous is not None:
                    speed = min(math.dist(center, previous) * sequence.fps, 5.0) / 5.0
                if item.present:
                    last_centers[obj] = center

                row.extend(
                    [
                        min(item.count, 3.0) / 3.0,
                        item.cx,
                        item.cy,
                        item.area,
                        speed,
                    ]
                )
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

        return ModelInput(features=rows, feature_names=feature_names, timestamps=timestamps, fps=sequence.fps)

    @staticmethod
    def feature_names() -> list[str]:
        """返回固定列名，便于排查模型输入。"""
        names: list[str] = []
        for obj in OBJECTS:
            names.extend([f"{obj}_count", f"{obj}_cx", f"{obj}_cy", f"{obj}_area", f"{obj}_speed"])
        for left, right in PAIR_FEATURES:
            names.extend([f"{left}_to_{right}_valid", f"{left}_to_{right}_dist"])
        names.extend(["t_norm", "t_sin", "t_cos"])
        return names

    def _collect_frame_stats(self, detections: list[Detection]) -> dict[str, _ObjectStats]:
        """聚合单帧检测框。

        同一类别多框时，按检测框面积加权计算中心点和平均面积；count 保留目标数量。
        """
        buckets: dict[str, list[tuple[float, float, float]]] = {name: [] for name in OBJECTS}
        for det in detections:
            obj = OBJECT_ALIASES.get(str(det.class_name))
            if obj is None:
                continue
            cx, cy, area = self._bbox_to_center_area(det)
            buckets[obj].append((cx, cy, area))

        stats: dict[str, _ObjectStats] = {}
        for obj, rows in buckets.items():
            if not rows:
                continue
            count = float(len(rows))
            weight_sum = sum(max(area, 1e-6) for _, _, area in rows)
            cx = sum(cx * max(area, 1e-6) for cx, _, area in rows) / weight_sum
            cy = sum(cy * max(area, 1e-6) for _, cy, area in rows) / weight_sum
            area = sum(area for _, _, area in rows) / count
            stats[obj] = _ObjectStats(count=count, cx=cx, cy=cy, area=area)
        return stats

    def _bbox_to_center_area(self, det: Detection) -> tuple[float, float, float]:
        """后端 Detection.bbox 为像素 xyxy，这里转成 0-1 归一化中心和面积。"""
        if len(det.bbox) < 4:
            return 0.0, 0.0, 0.0
        x1, y1, x2, y2 = [float(v) for v in det.bbox[:4]]
        width = max(0.0, x2 - x1)
        height = max(0.0, y2 - y1)
        cx = (x1 + x2) * 0.5 / self.frame_width
        cy = (y1 + y2) * 0.5 / self.frame_height
        area = (width / self.frame_width) * (height / self.frame_height)
        return min(max(cx, 0.0), 1.0), min(max(cy, 0.0), 1.0), min(max(area, 0.0), 1.0)
