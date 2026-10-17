"""CLEAN 历史离线策略 mstcn2-bundle（YAML ``offline.class`` 指向 ``CleanMSTCN2BundleSegmenter``）。

    offline:
      class: app.services.inference.offline.impl.clean_mstcn2_bundle.segmenter.CleanMSTCN2BundleSegmenter
      params: {model_dir: ${CLEANSIGHT_MODEL_PATH:./app/data}/clean-offline-mstcn2-bundle}

物料 ``model_dir/model.pt``（格式见 contract.py）。输入契约：训练帧率 15 fps（快于它按 ts 抽稀；慢于它照常跑，只告警）。
检测按训练框文件口径还原（面积 top-K、6 位小数、置信度恒 1）→ 94 维 → 整段 MS-TCN2 → pp20（短于 20 帧的段并入相邻较长段）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from app.types.detection import FrameDetection
from app.types.temporal import LabelProbs, TemporalSegment
from app.services.inference.offline.segmenter import OfflineSegmenter
from app.services.inference.resample import input_fps, resample_by_ts

from .bundle import bundle_columns
from .contract import ACTION_NAMES, FEATURE_COLUMNS, MODEL_FILE
from .frames import FPS, detection_rows
from .postprocess import PP_MIN_LEN, merge_short_segments

logger = logging.getLogger(__name__)

WIDTH, HEIGHT = 640, 480  # 检测框归一化用的画面尺寸（训练视频 640×480）
_RATE_SLACK = 0.01  # 帧率比较的相对余量，吸收 ts 舍入与抖动（同 resample）


@dataclass(frozen=True)
class BundleInput:
    ts: np.ndarray        # [T] float64，帧捕获 ts
    features: np.ndarray  # [T, 94] float32


class CleanMSTCN2BundleSegmenter(OfflineSegmenter):
    """ama-v5-bundle-94d 特征 + 整段 MS-TCN2 + pp20。"""

    labels = ACTION_NAMES

    def __init__(self, model_dir: str, *, min_segment_frames: int = PP_MIN_LEN):
        if int(min_segment_frames) < 0:
            raise ValueError(f"min_segment_frames 不能为负: {min_segment_frames}")
        self.model_dir = Path(model_dir)
        self.min_segment_frames = int(min_segment_frames)
        self._last_probs: Optional[LabelProbs] = None

    def materials(self) -> Sequence[Path]:
        return (self.model_dir / MODEL_FILE,)

    def preprocess(self, frames: Sequence[FrameDetection]) -> BundleInput:
        _warn_low_rate(frames)
        frames = resample_by_ts(frames, FPS)
        ts = np.asarray([f.ts for f in frames], dtype=np.float64)
        if len(frames) == 0:
            return BundleInput(ts=ts, features=np.zeros((0, len(FEATURE_COLUMNS)), dtype=np.float32))
        cols = bundle_columns(detection_rows(frames, width=WIDTH, height=HEIGHT))
        return BundleInput(ts=ts, features=np.stack([cols[c] for c in FEATURE_COLUMNS], axis=1).astype(np.float32))

    def segment(self, model_input: BundleInput) -> List[TemporalSegment]:
        if len(model_input.ts) == 0:
            return []
        path = self.model_dir / MODEL_FILE
        if not path.exists():
            raise FileNotFoundError(f"clean 离线模型权重不存在: {path}")
        from .model import load_model, predict_probs  # torch 只在真正前向时拉起

        probs = predict_probs([load_model(path)], model_input.features)
        labels = merge_short_segments(probs.argmax(axis=1), self.min_segment_frames)
        self._last_probs = LabelProbs(ts=model_input.ts, probs=probs, labels=self.labels)
        return self._segments(model_input.ts, labels, probs)

    def label_probs(self) -> Optional[LabelProbs]:
        return self._last_probs

    def _segments(self, ts: np.ndarray, labels: np.ndarray, probs: np.ndarray) -> List[TemporalSegment]:
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


def _warn_low_rate(frames: Sequence[FrameDetection]) -> None:
    """检测帧率随在线负载波动：低于训练帧率只告警、不失败（按帧计的窗口与速度会相应拉长）。"""
    rate = input_fps(frames)
    if rate is None or rate >= FPS * (1 - _RATE_SLACK):
        return
    logger.warning("[CleanMSTCN2BundleSegmenter] 输入帧率 %.3ffps 低于训练帧率 %.1ffps，照常推理", rate, FPS)
