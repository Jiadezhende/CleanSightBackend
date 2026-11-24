"""按训练列序组装 S1 几何特征和同帧的手部嵌入均值。"""

from __future__ import annotations

import numpy as np

from .bundle import bundle_columns
from .contract import EMBED_DIM, FEATURE_DIM, GEOMETRY_COLUMNS


def build_features(
    rows: list[list[tuple]], hand_counts: list[int], embeddings: np.ndarray,
) -> np.ndarray:
    """每帧一行。嵌入按帧、帧内手框顺序展开，无手帧的视觉特征为零。"""
    frame_count = len(rows)
    if not frame_count:
        return np.zeros((0, FEATURE_DIM), dtype=np.float32)

    frame_indices = np.repeat(np.arange(frame_count), hand_counts)
    if embeddings.shape != (len(frame_indices), EMBED_DIM):
        raise ValueError(f"手部嵌入形状 {embeddings.shape}，应为 {(len(frame_indices), EMBED_DIM)}")
    means = np.zeros((frame_count, EMBED_DIM), dtype=np.float32)
    np.add.at(means, frame_indices, embeddings.astype(np.float32))
    means /= np.asarray(hand_counts, dtype=np.float32).clip(min=1)[:, None]

    columns = bundle_columns(rows)
    geometry = np.stack([columns[name] for name in GEOMETRY_COLUMNS], axis=1).astype(np.float32)
    return np.concatenate((geometry, means), axis=1)
