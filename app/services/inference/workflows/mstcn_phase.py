"""TemporalAnalyzer that runs MS-TCN++ phase recognition on a detection window."""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from app.services.inference.data_models import AlarmInfo, DetectionOutput
from app.services.inference.workflows.analyzer import TemporalAnalyzer
from app.services.inference.workflows.mstcn_features import (
    DEFAULT_CLASS_ORDER,
    window_to_mstcn_features,
)
from app.services.inference.workflows.mstcn_runtime import MSTCNRuntime

logger = logging.getLogger(__name__)


class MSTCNPhaseAnalyzer(TemporalAnalyzer):
    """Recognize cleaning phases from a detector's recent result window.

    This analyzer is intentionally read-only in the first integration step:
    it emits frontend temporal events but returns no alarms.  Once the phase
    signal is validated online, business-rule alarms can be layered on top.
    """

    def __init__(
        self,
        name: str = "mstcn_phase",
        source_task_name: str = "clean_tool",
        model_path: str = "./MS-TCN2/models/Endo_Project/split_1/epoch-50.model",
        mapping_path: str = "./MS-TCN2/data/Endo_Project/mapping.txt",
        feature_dim: int = 20,
        min_frames: int = 30,
        max_frames: int = 300,
        image_width: int = 640,
        image_height: int = 480,
        class_order: Sequence[str] = DEFAULT_CLASS_ORDER,
        class_aliases: Optional[Mapping[str, str]] = None,
        num_layers_pg: int = 10,
        num_layers_r: int = 10,
        num_r: int = 3,
        num_f_maps: int = 64,
        device: str = "auto",
    ):
        super().__init__(name=name)
        self.source_task_name = source_task_name
        self.min_frames = min_frames
        self.max_frames = max_frames
        self.image_size: Tuple[int, int] = (image_width, image_height)
        self.class_order = tuple(class_order)
        self.class_aliases = dict(class_aliases or {})
        self.runtime = MSTCNRuntime(
            model_path=model_path,
            mapping_path=mapping_path,
            feature_dim=feature_dim,
            num_layers_pg=num_layers_pg,
            num_layers_r=num_layers_r,
            num_r=num_r,
            num_f_maps=num_f_maps,
            device=device,
        )
        self._sm: Dict[str, Any] = {
            "last_ts": 0.0,
            "last_label": None,
            "last_confidence": 0.0,
            "last_window_ts": 0.0,
            "frame_buffer": [],
        }

    def analyze_temporal(
        self,
        window: List[DetectionOutput],
    ) -> Tuple[List[str], List[AlarmInfo]]:
        if not window:
            return [], []

        buffer = self._advance_buffer(window)
        if len(buffer) < self.min_frames:
            return [], []

        latest_ts = buffer[-1].timestamp
        if latest_ts <= self._sm["last_window_ts"]:
            return self._format_event(), []

        features = window_to_mstcn_features(
            buffer,
            image_size=self.image_size,
            class_order=self.class_order,
            class_aliases=self.class_aliases,
        )
        try:
            result = self.runtime.predict(features)
        except Exception as e:
            logger.error("[MSTCNPhaseAnalyzer] predict failed: %s", e, exc_info=True)
            return [f"mstcn_phase_error={type(e).__name__}"], []

        self._sm["last_label"] = result["current_label"]
        self._sm["last_confidence"] = float(result["confidence"])
        self._sm["last_window_ts"] = latest_ts
        self._sm["last_sequence_len"] = len(result["labels"])
        return self._format_event(), []

    def _advance_buffer(self, window: List[DetectionOutput]) -> List[DetectionOutput]:
        """Append only unseen frames and keep the analyzer-owned sequence window."""
        last_ts = float(self._sm["last_ts"])
        new_frames = [output for output in window if output.timestamp > last_ts]
        if new_frames:
            new_frames.sort(key=lambda output: output.timestamp)
            self._sm["frame_buffer"].extend(new_frames)
            self._sm["last_ts"] = new_frames[-1].timestamp

        if self.max_frames > 0 and len(self._sm["frame_buffer"]) > self.max_frames:
            self._sm["frame_buffer"] = self._sm["frame_buffer"][-self.max_frames :]

        return list(self._sm["frame_buffer"])

    def _format_event(self) -> List[str]:
        label = self._sm.get("last_label")
        if not label:
            return []
        confidence = float(self._sm.get("last_confidence", 0.0))
        return [f"mstcn_phase={label} conf={confidence:.2f}"]
