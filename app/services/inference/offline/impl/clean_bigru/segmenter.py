"""CLEAN 历史离线策略 BiGRU（YAML ``offline.class`` 指向 ``CleanBiGRUSegmenter``）。

    offline:
      class: app.services.inference.offline.impl.clean_bigru.segmenter.CleanBiGRUSegmenter
      params:
        model_dir: ${CLEANSIGHT_MODEL_PATH:./app/data}/clean-offline-bigru
        min_duration_s: 0.2

物料 ``model_dir/model.pt``（格式见 contract.py）。逐帧 argmax → 合并连续同类帧（跳过 idle、丢弃短于 ``min_duration_s`` 的段）。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, List, Optional, Sequence, Tuple

import numpy as np

from app.types.detection import FrameDetection
from app.types.temporal import LabelProbs, TemporalSegment
from app.services.inference.offline.segmenter import OfflineSegmenter

from .contract import FEATURE_DIM, FEATURE_VERSION, LABELS, MODEL_FILE
from .features import ModelInput, build_features


class CleanBiGRUSegmenter(OfflineSegmenter):
    """BiGRU 整段离线模型（clean_bbox_v2_top1_impute+center_window+business_priors，249 维）。"""

    model_version = "clean_bigru_v1"
    labels: Tuple[str, ...] = LABELS

    def __init__(
        self,
        model_dir: str,
        min_duration_s: float = 0.2,
        fps: float = 7.5,
        frame_width: int = 640,
        frame_height: int = 480,
    ):
        self.model_dir = Path(model_dir)
        self.min_duration_s = max(0.0, float(min_duration_s))
        self.fps = float(fps)  # 只作 ts 不可用时估 speed 的兜底帧率
        self.frame_width = max(1, int(frame_width))
        self.frame_height = max(1, int(frame_height))
        self._model = None
        self._normalizer: Tuple[Any, Any] | None = None
        self._last_probs: LabelProbs | None = None

    def materials(self) -> Sequence[Path]:
        return (self.model_dir / MODEL_FILE,)

    def preprocess(self, frames: Sequence[FrameDetection]) -> ModelInput:
        return build_features(frames, self.fps, self.frame_width, self.frame_height)

    def segment(self, model_input: ModelInput) -> List[TemporalSegment]:
        """跑模型得到逐帧标签，解码成 TemporalSegment。"""
        if model_input.frame_count == 0:
            return []
        if model_input.feature_dim != FEATURE_DIM or model_input.feature_version != FEATURE_VERSION:
            raise AssertionError(f"特征 {model_input.feature_version}/{model_input.feature_dim} 与契约不符")

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
        """惰性加载权重，归一化+finite 兜底后前向，返回逐帧 softmax `[T, len(self.labels)]`。"""
        import torch

        if self._model is None:
            self._load_model(model_input)

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
        return probs.astype(np.float32)

    def _load_model(self, model_input: ModelInput) -> None:
        """加载带元数据的 checkpoint：校验 class_names / feature_names / feature_version，取出 normalizer。"""
        path = self.model_dir / MODEL_FILE
        if not path.exists():
            raise FileNotFoundError(f"clean 离线模型权重不存在: {path}")
        import torch

        from .model import build_model

        # checkpoint 含 numpy normalizer，PyTorch 2.6 起默认 weights_only=True 会拒读（本地可信训练产物）
        checkpoint = torch.load(path, map_location="cpu", weights_only=False)
        model = build_model(model_input.feature_dim, len(self.labels))
        model.load_state_dict(checkpoint.get("state_dict", checkpoint), strict=True)

        class_names = checkpoint.get("class_names")
        if class_names is not None and tuple(class_names) != self.labels:
            raise ValueError(f"clean 离线模型 class_names 与契约类序不一致: {list(class_names)}")
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
