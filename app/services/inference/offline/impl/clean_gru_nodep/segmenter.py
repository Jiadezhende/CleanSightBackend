"""CLEAN 历史离线策略：因果滑窗 GRU（YAML ``offline.class`` 指向 ``CleanGRUNodepSegmenter``）。

    offline:
      class: app.services.inference.offline.impl.clean_gru_nodep.segmenter.CleanGRUNodepSegmenter
      params:
        model_dir: ${CLEANSIGHT_MODEL_PATH:./app/data}/clean-offline-gru-nodep
        model_input_fps: 7.5
        confidence_override: 1.0
        min_duration_s: 0.2

``model_input_fps`` 须等于训练帧率、``confidence_override`` 须与训练标注口径一致（训练标注无置信度列 → 1.0）——配错不报错、
结果静默变差。逐帧 argmax → 合并连续同类帧（跳过 idle、丢弃短于 ``min_duration_s`` 的段）。
"""

from __future__ import annotations

from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import numpy as np

from app.types.detection import FrameDetection
from app.types.temporal import LabelProbs, TemporalSegment
from app.services.inference.offline.segmenter import OfflineSegmenter
from app.services.inference.resample import resample_by_ts

from .contract import FEATURE_DIM, LABELS, MODEL_FILE, STRUCTURE, WINDOW
from .features import ModelInput, build_nodep_concat_features


class CleanGRUNodepSegmenter(OfflineSegmenter):
    """因果滑窗 GRU 离线模型（ama-v3-concat23-nodep-226d，6 类）。"""

    model_version = "clean_gru_nodep226d"
    labels: Tuple[str, ...] = LABELS

    def __init__(
        self,
        model_dir: str,
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
        self.model_dir = Path(model_dir)
        self.fps = float(model_input_fps)
        self.min_duration_s = max(0.0, float(min_duration_s))
        self.confidence_override = None if confidence_override is None else float(confidence_override)
        self.frame_width = max(1, int(frame_width))
        self.frame_height = max(1, int(frame_height))
        self._model = None
        self._last_probs: LabelProbs | None = None

    def materials(self) -> Sequence[Path]:
        return (self.model_dir / MODEL_FILE,)

    def preprocess(self, frames: Sequence[FrameDetection]) -> ModelInput:
        """按 ts 降采样到 model_input_fps（只挑真实帧，ts 与 detections.jsonl 位级相等）→ 226 维特征。

        检测帧率低于 model_input_fps 即 ValueError（strict）：只能降采样，放行会静默错配时间密度。
        """
        return build_nodep_concat_features(
            resample_by_ts(frames, self.fps, strict=True),
            self.fps,
            self.frame_width,
            self.frame_height,
            self.confidence_override,
        )

    def segment(self, model_input: ModelInput) -> List[TemporalSegment]:
        """跑模型得到逐帧标签，解码成 TemporalSegment。"""
        if model_input.frame_count == 0:
            return []

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

    def label_probs(self) -> Optional[LabelProbs]:
        """最近一次 segment() 的逐帧 softmax（未跑过为 None），供可视化旁路落盘。"""
        return self._last_probs

    def _predict_with_model(self, model_input: ModelInput) -> np.ndarray:
        """每帧取以它为末帧的窗口前向，返回逐帧 softmax `[T, len(self.labels)]`。"""
        import torch

        from .model import WINDOW_BATCH, causal_windows

        if self._model is None:
            self._load_model()
        windows = causal_windows(np.asarray(model_input.features, dtype=np.float32), WINDOW)
        self._model.eval()
        chunks = []
        with torch.no_grad():
            for start in range(0, len(windows), WINDOW_BATCH):
                batch = torch.from_numpy(np.ascontiguousarray(windows[start:start + WINDOW_BATCH]))
                chunks.append(torch.softmax(self._model(batch), dim=-1).numpy())
        return np.concatenate(chunks, axis=0).astype(np.float32)

    def _load_model(self) -> None:
        """strict 加载训练状态 checkpoint 的 `model_state`。"""
        path = self.model_dir / MODEL_FILE
        if not path.exists():
            raise FileNotFoundError(f"clean 离线模型权重不存在: {path}")
        import torch

        from .model import build_model

        checkpoint = torch.load(path, map_location="cpu", weights_only=True)
        model = build_model(FEATURE_DIM, len(self.labels), **STRUCTURE)
        model.load_state_dict(checkpoint["model_state"], strict=True)
        self._model = model

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
