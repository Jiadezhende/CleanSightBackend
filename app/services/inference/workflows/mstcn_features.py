"""Feature adapter from DetectionOutput windows to MS-TCN++ input arrays."""

from __future__ import annotations

from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import numpy as np

from app.services.inference.data_models import Detection, DetectionOutput


DEFAULT_CLASS_ORDER: Tuple[str, ...] = (
    "Hand",
    "Long_Brush_Head",
    "Scope_Port",
    "Short_Brush",
)

# Common aliases to tolerate detector naming differences without retraining.
DEFAULT_CLASS_ALIASES: Mapping[str, str] = {
    "hand": "Hand",
    "long_brush_head": "Long_Brush_Head",
    "long brush head": "Long_Brush_Head",
    "scope_port": "Scope_Port",
    "scope port": "Scope_Port",
    "short_brush": "Short_Brush",
    "short brush": "Short_Brush",
}


def window_to_mstcn_features(
    window: Sequence[DetectionOutput],
    image_size: Tuple[int, int] = (640, 480),
    class_order: Sequence[str] = DEFAULT_CLASS_ORDER,
    class_aliases: Optional[Mapping[str, str]] = None,
) -> np.ndarray:
    """Convert a temporal detection window to ``[feature_dim, T]`` features.

    The offline extractor keeps the highest-confidence detection per class and
    writes ``[cx, cy, w, h, conf]`` in normalized coordinates.  This function
    mirrors that contract for online inference.
    """
    aliases = dict(DEFAULT_CLASS_ALIASES)
    if class_aliases:
        aliases.update(class_aliases)

    rows: List[np.ndarray] = []
    for output in window:
        width, height = _resolve_image_size(output, image_size)
        frame_feat = np.zeros(len(class_order) * 5, dtype=np.float32)
        for class_name, det in _best_detection_by_class(
            output.detections, class_order, aliases
        ).items():
            cls_idx = class_order.index(class_name)
            start = cls_idx * 5
            frame_feat[start : start + 5] = _detection_to_xywhn(det, width, height)
        rows.append(frame_feat)

    if not rows:
        return np.zeros((len(class_order) * 5, 0), dtype=np.float32)
    return np.asarray(rows, dtype=np.float32).T


def _best_detection_by_class(
    detections: Iterable[Detection],
    class_order: Sequence[str],
    class_aliases: Mapping[str, str],
) -> Dict[str, Detection]:
    known = set(class_order)
    best: Dict[str, Detection] = {}
    for det in detections:
        class_name = _canonical_class_name(det.class_name, class_aliases)
        if class_name not in known:
            continue
        old = best.get(class_name)
        if old is None or det.confidence > old.confidence:
            best[class_name] = det
    return best


def _canonical_class_name(class_name: str, aliases: Mapping[str, str]) -> str:
    if class_name in aliases.values():
        return class_name
    return aliases.get(str(class_name).strip().lower(), class_name)


def _resolve_image_size(
    output: DetectionOutput, fallback: Tuple[int, int]
) -> Tuple[float, float]:
    shape = output.metadata.get("frame_shape") if output.metadata else None
    if isinstance(shape, (list, tuple)) and len(shape) >= 2:
        height, width = float(shape[0]), float(shape[1])
        if width > 0 and height > 0:
            return width, height
    return float(fallback[0]), float(fallback[1])


def _detection_to_xywhn(det: Detection, width: float, height: float) -> np.ndarray:
    x1, y1, x2, y2 = [float(v) for v in det.bbox]
    box_w = max(0.0, x2 - x1)
    box_h = max(0.0, y2 - y1)
    cx = x1 + box_w / 2.0
    cy = y1 + box_h / 2.0
    values = np.asarray(
        [
            cx / width,
            cy / height,
            box_w / width,
            box_h / height,
            float(det.confidence),
        ],
        dtype=np.float32,
    )
    return np.clip(values, 0.0, 1.0)
