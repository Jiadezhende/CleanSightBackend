"""CLEAN 历史离线策略 mstcn2-bundle（YAML ``offline.class`` 指向 ``CleanMSTCN2BundleSegmenter``）。

    offline:
      class: app.services.inference.offline.segmentation.impl.clean_mstcn2_bundle.segmenter.CleanMSTCN2BundleSegmenter
      params:
        model_dir: ${CLEANSIGHT_MODEL_PATH:./app/data}/clean-offline-mstcn2-bundle

物料 ``model_dir/model.pt``（格式见 contract.py）。输入契约：训练帧率 15 fps（快于它按 ts 抽稀；慢于它照常跑，只告警）。
检测按训练框文件口径还原（面积 top-K、6 位小数、置信度恒 1）→ 94 维 → 整段 MS-TCN2 → pp20（短于 20 帧的段并入相邻较长段）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List, Sequence

import numpy as np

from app.types.detection import FrameDetection
from app.types.temporal import LabelProbs, TemporalSegment
from app.services.inference.offline.segmentation.segmenter import OfflineContext, OfflineSegmenter
from app.services.inference.offline.segmentation.utils import FrameInputContract

from .bundle import bundle_columns
from .contract import ACTION_NAMES, FEATURE_COLUMNS, MODEL_FILE
from .frames import FPS, detection_rows
from .postprocess import PP_MIN_LEN, merge_short_segments

WIDTH, HEIGHT = 640, 480  # 检测框归一化用的画面尺寸（训练视频 640×480）


@dataclass(frozen=True)
class BundleInput:
    ts: np.ndarray        # [T] float64，帧捕获 ts
    features: np.ndarray  # [T, 94] float32


class CleanMSTCN2BundleSegmenter(OfflineSegmenter[BundleInput]):
    """ama-v5-bundle-94d 特征 + 整段 MS-TCN2 + pp20。"""

    frame_contract = FrameInputContract(target_fps=FPS)
    labels = ACTION_NAMES

    def __init__(self, model_dir: str, *, min_segment_frames: int = PP_MIN_LEN):
        if int(min_segment_frames) < 0:
            raise ValueError(f"min_segment_frames 不能为负: {min_segment_frames}")
        self.model_dir = Path(model_dir)
        self.min_segment_frames = int(min_segment_frames)

    def materials(self) -> Sequence[Path]:
        return (self.model_dir / MODEL_FILE,)

    def _build_model_input(self, frames: Sequence[FrameDetection], context: OfflineContext) -> BundleInput:
        ts = np.asarray([f.ts for f in frames], dtype=np.float64)
        cols = bundle_columns(detection_rows(frames, width=WIDTH, height=HEIGHT))
        return BundleInput(ts=ts, features=np.stack([cols[c] for c in FEATURE_COLUMNS], axis=1).astype(np.float32))

    def _predict(self, model_input: BundleInput) -> LabelProbs:
        path = self.model_dir / MODEL_FILE
        if not path.exists():
            raise FileNotFoundError(f"clean 离线模型权重不存在: {path}")
        from .model import load_model, predict_probs  # torch 只在真正前向时拉起

        probs = predict_probs(load_model(path), model_input.features)
        return LabelProbs(ts=model_input.ts, probs=probs, labels=self.labels)

    def _postprocess(self, label_probs: LabelProbs) -> List[TemporalSegment]:
        probs = label_probs.probs
        labels = merge_short_segments(probs.argmax(axis=1), self.min_segment_frames)
        return self._labels_to_segments(label_probs.ts, labels, probs)

    def _labels_to_segments(self, ts: np.ndarray, labels: np.ndarray, probs: np.ndarray) -> List[TemporalSegment]:
        """连续同标签帧 → 一段（跳过 idle）；conf = 段内该标签概率的均值。"""
        bounds = np.flatnonzero(np.diff(labels)) + 1
        starts = np.concatenate([[0], bounds])
        ends = np.concatenate([bounds, [labels.size]])
        out = []
        for s, e in zip(starts, ends):
            label = int(labels[s])
            if label == 0:
                continue
            out.append(TemporalSegment(
                producer=self.name,
                label=self.labels[label],
                start=float(ts[s]),
                end=float(ts[e - 1]),
                conf=min(1.0, max(0.0, float(probs[s:e, label].mean()))),
                meta={"model": self.model_dir.name},
            ))
        return out
