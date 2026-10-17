"""后处理：最短时长合并（pp20）。

pp20 只按段长合并：反复把最短的、短于 ``min_len`` 帧的段并入左右相邻段中较长的一段，直到没有短段。
不使用顺序、交替、典型段长等先验（识别层约定）。副作用是会吞掉真实存在的短于 1.33 s 的动作段，
需要判断很短的动作时请用 raw。
"""

from __future__ import annotations

import numpy as np

PP_MIN_LEN = 20  # 15 fps 下 1.33 s


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
