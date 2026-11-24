"""反复把短于最短时长的段并入相邻较长的段，与训练侧 pp5 的解码一致。"""

from __future__ import annotations

import numpy as np

PP_MIN_LEN = 5  # 15 fps 下约 0.33 s


def merge_short_segments(labels: np.ndarray, min_len: int) -> np.ndarray:
    out = labels.copy()
    if min_len <= 1:
        return out
    while True:
        bounds = np.flatnonzero(np.diff(out)) + 1
        starts = np.concatenate([[0], bounds])
        ends = np.concatenate([bounds, [out.size]])
        lengths = ends - starts
        if len(starts) <= 1 or lengths.min() >= min_len:
            return out
        i = int(np.argmin(lengths))
        left = lengths[i - 1] if i > 0 else -1
        right = lengths[i + 1] if i + 1 < len(starts) else -1
        donor = i - 1 if left >= right else i + 1
        out[starts[i]:ends[i]] = out[starts[donor]]
