"""CLEAN stage 离线模型策略。

本文件保持“单策略文件自包含”：
    - clean 专属特征转换；
    - 三种可路由离线模型结构；
    - 模型输出到 SegmentFact 的解码逻辑。

输入:
    OfflineRunner 从 FeatureStore.load_many(task_id, step_id, subscribes)
    读取 Mapping[source, Sequence[FrameDetections]]。

输出:
    List[SegmentFact]，由 Runner 校验并幂等写入 FactLedger。

注意:
    这里不包含训练流程。训练仍在独立 offline-model 仓内完成，后端只负责加载
    已训练权重并执行离线推理。若未配置 model_path，本策略可按 fallback_to_rules
    使用轻量规则输出，便于本地回环测试；生产配置应显式提供权重路径。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from app.domain.detection import Detection, FrameDetections
from app.services.inference.models import SegmentFact
from app.services.inference.offline.segmenter import OfflineSegmenter


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
        [T, F] 数值特征矩阵。当前 F=68，包含 hand top-2、其它目标几何/速度、
        目标对距离和时间位置编码。
    feature_names:
        features 每一列的名字，便于训练仓和后端排查对齐问题。
    timestamps:
        每一行特征对应的原始帧时间戳。
    fps:
        兜底采样率。speed 优先用真实 timestamp 的 dt 计算，dt 异常时才用 fps。
    """

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
class _ObjectBox:
    cx: float
    cy: float
    area: float
    score: float


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
    """把 clean 检测框序列转换成固定维时序特征。

    hand 使用 top-2 规则：
        两只手是不同实体，不能简单面积加权成一个中心点。这里每帧按 score 选出
        最可信的两个 hand，分别写入 hand_top1 / hand_top2 槽位。

    其它对象:
        同类多框按 confidence * area 加权聚合，保留 count/cx/cy/area/speed。

    speed:
        使用当前帧和该对象上一次出现帧的真实 timestamp 差值 dt 计算；
        dt 非法时才使用 fps 兜底。
    """

    def __init__(self, frame_width: int = 640, frame_height: int = 480):
        self.frame_width = max(1, int(frame_width))
        self.frame_height = max(1, int(frame_height))

    def transform(self, frames: Sequence[FrameDetections], fps: float) -> ModelInput:
        feature_names = self.feature_names()
        timestamps = [float(f.timestamp) for f in frames]
        frame_count = max(1, len(frames))
        default_dt = 1.0 / max(float(fps), 1e-6)

        last_center: Dict[str, Tuple[float, float] | None] = {}
        last_ts: Dict[str, float] = {}
        rows: List[List[float]] = []

        for idx, frame in enumerate(frames):
            ts = float(frame.timestamp)
            boxes = self._collect_boxes(frame)
            hand_slots = self._top_hand_slots(boxes.get("hand", []))
            object_stats = self._collect_object_stats(boxes)

            row: List[float] = []
            centers: Dict[str, List[Tuple[float, float]]] = {}
            present: Dict[str, bool] = {}

            hand_count = float(len(boxes.get("hand", [])))
            row.append(min(hand_count, 3.0) / 3.0)
            present["hand"] = hand_count > 0
            centers["hand"] = []
            for slot_idx, slot in enumerate(hand_slots, start=1):
                key = f"hand_top{slot_idx}"
                speed = self._speed(key, slot, ts, last_center, last_ts, default_dt)
                row.extend([1.0 if slot.present else 0.0, slot.cx, slot.cy, slot.area, speed])
                if slot.present:
                    centers["hand"].append((slot.cx, slot.cy))

            for obj in OBJECTS:
                if obj == "hand":
                    continue
                stat = object_stats.get(obj, _ObjectStats())
                speed = self._speed(obj, stat, ts, last_center, last_ts, default_dt)
                row.extend([min(stat.count, 3.0) / 3.0, stat.cx, stat.cy, stat.area, speed])
                present[obj] = stat.present
                centers[obj] = [(stat.cx, stat.cy)] if stat.present else []

            for left, right in PAIR_FEATURES:
                valid = 1.0 if present.get(left) and present.get(right) else 0.0
                dist = 0.0
                if valid:
                    dist = self._min_center_distance(centers[left], centers[right])
                row.extend([valid, dist])

            t_norm = 0.0 if frame_count <= 1 else idx / (frame_count - 1)
            row.extend([t_norm, math.sin(2 * math.pi * t_norm), math.cos(2 * math.pi * t_norm)])
            rows.append(row)

        return ModelInput(features=rows, feature_names=feature_names, timestamps=timestamps, fps=float(fps))

    @staticmethod
    def feature_names() -> List[str]:
        names = ["hand_count"]
        for slot in ("hand_top1", "hand_top2"):
            names.extend([f"{slot}_present", f"{slot}_cx", f"{slot}_cy", f"{slot}_area", f"{slot}_speed"])
        for obj in OBJECTS:
            if obj == "hand":
                continue
            names.extend([f"{obj}_count", f"{obj}_cx", f"{obj}_cy", f"{obj}_area", f"{obj}_speed"])
        for left, right in PAIR_FEATURES:
            names.extend([f"{left}_to_{right}_valid", f"{left}_to_{right}_dist"])
        names.extend(["t_norm", "t_sin", "t_cos"])
        return names

    def _collect_boxes(self, frame: FrameDetections) -> Dict[str, List[_ObjectBox]]:
        width, height = self._frame_size(frame)
        out: Dict[str, List[_ObjectBox]] = {name: [] for name in OBJECTS}
        for det in frame.detections:
            obj = OBJECT_ALIASES.get(str(det.class_name))
            if obj is None:
                continue
            cx, cy, area = self._bbox_to_center_area(det, width, height)
            score = max(0.0, float(det.confidence)) * max(area, 1e-6)
            out[obj].append(_ObjectBox(cx=cx, cy=cy, area=area, score=score))
        return out

    def _frame_size(self, frame: FrameDetections) -> Tuple[int, int]:
        meta = frame.metadata or {}
        width = meta.get("frame_width") or meta.get("width") or self.frame_width
        height = meta.get("frame_height") or meta.get("height") or self.frame_height
        return max(1, int(width)), max(1, int(height))

    def _bbox_to_center_area(self, det: Detection, width: int, height: int) -> Tuple[float, float, float]:
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

    @staticmethod
    def _top_hand_slots(hands: Sequence[_ObjectBox]) -> List[_ObjectStats]:
        selected = sorted(hands, key=lambda b: b.score, reverse=True)[:2]
        slots = [
            _ObjectStats(count=1.0, cx=box.cx, cy=box.cy, area=box.area)
            for box in selected
        ]
        while len(slots) < 2:
            slots.append(_ObjectStats())
        return slots

    @staticmethod
    def _collect_object_stats(boxes: Dict[str, List[_ObjectBox]]) -> Dict[str, _ObjectStats]:
        stats: Dict[str, _ObjectStats] = {}
        for obj, entries in boxes.items():
            if obj == "hand" or not entries:
                continue
            weight_sum = sum(max(b.score, 1e-6) for b in entries)
            cx = sum(b.cx * max(b.score, 1e-6) for b in entries) / weight_sum
            cy = sum(b.cy * max(b.score, 1e-6) for b in entries) / weight_sum
            area = sum(b.area * max(b.score, 1e-6) for b in entries) / weight_sum
            stats[obj] = _ObjectStats(count=float(len(entries)), cx=cx, cy=cy, area=area)
        return stats

    @staticmethod
    def _speed(
        key: str,
        stat: _ObjectStats,
        ts: float,
        last_center: Dict[str, Tuple[float, float] | None],
        last_ts: Dict[str, float],
        default_dt: float,
    ) -> float:
        if not stat.present:
            return 0.0
        center = (stat.cx, stat.cy)
        previous = last_center.get(key)
        previous_ts = last_ts.get(key)
        speed = 0.0
        if previous is not None and previous_ts is not None:
            dt = ts - previous_ts
            if not math.isfinite(dt) or dt <= 1e-6:
                dt = default_dt
            speed = min(math.dist(center, previous) / max(dt, 1e-6), 5.0) / 5.0
        last_center[key] = center
        last_ts[key] = ts
        return speed

    @staticmethod
    def _min_center_distance(left: Sequence[Tuple[float, float]], right: Sequence[Tuple[float, float]]) -> float:
        if not left or not right:
            return 0.0
        dist = min(math.dist(a, b) for a in left for b in right)
        return min(dist, math.sqrt(2.0)) / math.sqrt(2.0)


class _RuleDecoder:
    """无权重时的轻量 fallback，只用于本地回环，不作为最终模型精度来源。"""

    def __init__(self, min_duration_s: float):
        self.min_duration_s = max(0.0, float(min_duration_s))

    def predict(self, model_input: ModelInput) -> Tuple[List[int], List[float]]:
        name_to_idx = {name: idx for idx, name in enumerate(model_input.feature_names)}
        labels: List[int] = []
        confs: List[float] = []
        for row in model_input.features:
            label, conf = self._predict_row(row, name_to_idx)
            labels.append(ACTION_LABELS.index(label))
            confs.append(conf)
        return labels, confs

    @staticmethod
    def _value(row: List[float], name_to_idx: Dict[str, int], name: str) -> float:
        idx = name_to_idx.get(name)
        if idx is None or idx >= len(row):
            return 0.0
        return float(row[idx])

    def _presence(self, row: List[float], name_to_idx: Dict[str, int], obj: str) -> float:
        if obj == "hand":
            return min(1.0, self._value(row, name_to_idx, "hand_count") * 3.0)
        return min(1.0, self._value(row, name_to_idx, f"{obj}_count") * 3.0)

    def _predict_row(self, row: List[float], name_to_idx: Dict[str, int]) -> Tuple[str, float]:
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

        if air_gun:
            return "air_injection", max(0.2, air_gun)
        if syringe:
            return "flush", max(0.2, syringe)
        if short_brush and max(hand, scope_control, scope_distal):
            return "short_brush_cleaning", max(0.2, min(short_brush, max(hand, scope_control, scope_distal)))
        if long_brush and max(hand, scope_mid, scope_distal):
            return "long_brush_insert", max(0.2, min(long_brush, max(hand, scope_mid, scope_distal)))
        return "idle", 1.0


class _CleanTorchSegmenter(OfflineSegmenter):
    """clean 模型策略基类：特征转换 + torch 模型加载 + SegmentFact 解码。"""

    model_version = "clean_model_v1"

    def __init__(
        self,
        name: str,
        subscribes: Sequence[str],
        model_path: str | None = None,
        min_duration_s: float = 0.2,
        fps: float = 7.5,
        frame_width: int = 640,
        frame_height: int = 480,
        fallback_to_rules: bool = True,
    ):
        super().__init__(name, subscribes)
        self.model_path = model_path
        self.min_duration_s = max(0.0, float(min_duration_s))
        self.fps = float(fps)
        self.vectorizer = FeatureVectorizer(frame_width, frame_height)
        self.fallback_to_rules = bool(fallback_to_rules)
        self._model = None
        self._normalizer: Tuple[Any, Any] | None = None
        self._last_result: dict | None = None

    def preprocess(self, streams: Mapping[str, Sequence[FrameDetections]]) -> ModelInput:
        by_ts: Dict[float, List[Detection]] = {}
        metadata_by_ts: Dict[float, dict] = {}
        for src in self.subscribes:
            for fd in streams.get(src, ()):
                ts = float(fd.timestamp)
                by_ts.setdefault(ts, []).extend(fd.detections)
                if fd.metadata:
                    metadata_by_ts.setdefault(ts, {}).update(fd.metadata)

        frames = [
            FrameDetections(detections=by_ts[ts], metadata=metadata_by_ts.get(ts, {}), timestamp=ts)
            for ts in sorted(by_ts)
        ]
        return self.vectorizer.transform(frames, self.fps)

    def segment(self, model_input: ModelInput) -> List[SegmentFact]:
        if model_input.frame_count == 0:
            return []

        if self.model_path:
            labels, confs = self._predict_with_model(model_input)
        elif self.fallback_to_rules:
            labels, confs = _RuleDecoder(self.min_duration_s).predict(model_input)
        else:
            raise ValueError(f"{type(self).__name__} 未配置 model_path，且 fallback_to_rules=false")

        segments = self._labels_to_segments(model_input.timestamps, labels, confs)
        self._last_result = {
            "model_version": self.model_version,
            "model_class": type(self).__name__,
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
        return self._last_result

    def _predict_with_model(self, model_input: ModelInput) -> Tuple[List[int], List[float]]:
        import numpy as np
        import torch

        if self._model is None:
            self._load_model(model_input.feature_dim, len(ACTION_LABELS))

        x_np = np.asarray(model_input.features, dtype=np.float32)
        if self._normalizer is not None:
            mean, std = self._normalizer
            x_np = (x_np - mean) / std
        x = torch.tensor(x_np[None, :, :], dtype=torch.float32)
        self._model.eval()
        with torch.no_grad():
            logits = self._model(x)[0].transpose(0, 1)
            probs = torch.softmax(logits, dim=-1).cpu().numpy()
        labels = probs.argmax(axis=1).astype("int64").tolist()
        confs = probs.max(axis=1).astype("float32").tolist()
        return labels, confs

    def _load_model(self, in_dim: int, class_count: int) -> None:
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
        self._model = self._build_model(in_dim, class_count)
        state_dict = checkpoint.get("state_dict", checkpoint)
        self._model.load_state_dict(state_dict, strict=True)

        feature_names = checkpoint.get("feature_names")
        if feature_names is not None and list(feature_names) != FeatureVectorizer.feature_names():
            raise ValueError("clean 离线模型 feature_names 与后端特征列不一致")

        mean = checkpoint.get("normalizer_mean")
        std = checkpoint.get("normalizer_std")
        if mean is not None and std is not None:
            self._normalizer = (mean, std)

    def _labels_to_segments(
        self, timestamps: Sequence[float], labels: Sequence[int], confs: Sequence[float]
    ) -> List[SegmentFact]:
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
        raise NotImplementedError


# ==================== 三种 clean 离线模型结构 ====================


class _DilatedResidualLayer:
    """延迟导入 torch 的 TCN 层工厂，避免 mock 路径导入 clean 时抢占重依赖。"""

    @staticmethod
    def make(channels: int, dilation: int, dropout: float):
        import torch.nn as nn

        class Layer(nn.Module):
            def __init__(self):
                super().__init__()
                self.net = nn.Sequential(
                    nn.Conv1d(channels, channels, kernel_size=3, padding=dilation, dilation=dilation),
                    nn.ReLU(),
                    nn.Dropout(dropout),
                    nn.Conv1d(channels, channels, kernel_size=1),
                )
                self.act = nn.ReLU()

            def forward(self, x):
                return self.act(x + self.net(x))

        return Layer()


def _make_mstcn_bilstm(in_dim: int, class_count: int, hidden: int = 64):
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
    """CLEAN 阶段 MS-TCN + BiLSTM 离线模型。"""

    model_version = "clean_mstcn_bilstm_v1"

    def _build_model(self, in_dim: int, class_count: int):
        return _make_mstcn_bilstm(in_dim, class_count)


class CleanASFormerSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 ASFormer 风格离线模型。"""

    model_version = "clean_asformer_v1"

    def _build_model(self, in_dim: int, class_count: int):
        return _make_asformer(in_dim, class_count)


class CleanBiGRUSegmenter(_CleanTorchSegmenter):
    """CLEAN 阶段 BiGRU 离线模型。"""

    model_version = "clean_bigru_v1"

    def _build_model(self, in_dim: int, class_count: int):
        return _make_bigru(in_dim, class_count)


# 兼容旧文档/旧测试中使用的 CleanSegmenter 名称；默认指向推荐离线 baseline。
CleanSegmenter = CleanMSTCNBiLSTMSegmenter
