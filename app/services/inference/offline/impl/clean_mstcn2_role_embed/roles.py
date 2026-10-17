"""角色判定（握镜手 / 送刷手）与送刷手多尺度速度。

与仓库 ``tools/feed_dir_features.py`` 同口径：

- **平滑**：每只手与前后 ±0.5 s（±7 帧）内各帧中离它最近（中心距离 < 0.12）的手相连，对这些手的
  p(握镜) 对数几率 log(p / (1 − p)) 取平均；
- **判定**：有 ≥ 2 只手时，平滑后最高的是握镜手、最低的是送刷手；只有 1 只手时，平均对数几率 ≥ 0
  （即 p ≥ 0.5）算握镜手，否则算送刷手；
- **速度**：送刷手框中心 ÷ 操作部框高（操作部框取每帧面积最大者，插值后 1 s 中值平滑），缺口 ≤ 8 帧
  线性插值，前后 2 / 5 / 10 / 20 帧中心差分，取模长（/秒），上限 4。

本实现的角色判定同时决定 ``slots.py`` 的槽位排序（槽位 1 = 握镜手，槽位 2 = 送刷手）。
"""

from __future__ import annotations

import numpy as np

from .frames import CTRL, FPS, HAND

SMOOTH_HALF = 7  # ±0.5 s
LINK_GATE = 0.12  # 图像归一化坐标
GAP_FILL = 8
SCALES = (2, 5, 10, 20)
CTRL_SMOOTH = 15
FALLBACK_H = 0.25


def fill(values, valid, max_gap=None):
    """有效帧之间线性插值（两端常值外推）；max_gap 给定时，超过该长度的缺口不算有效。返回 (filled, ok)。"""
    values = np.asarray(values, dtype=np.float64).reshape(len(valid), -1)
    idx = np.flatnonzero(valid)
    t = np.arange(len(valid))
    if idx.size == 0:
        return np.zeros_like(values), np.zeros(len(valid), bool)
    out = np.stack([np.interp(t, idx, values[idx, c]) for c in range(values.shape[1])], axis=1)
    if max_gap is None:
        return out, np.ones(len(valid), bool)
    ok = valid.copy()
    pos = np.searchsorted(idx, t)
    left = idx[np.clip(pos - 1, 0, idx.size - 1)]
    right = idx[np.clip(pos, 0, idx.size - 1)]
    inside = (t > idx[0]) & (t < idx[-1]) & ~valid
    ok |= inside & ((right - left - 1) <= max_gap)
    return out, ok


def median_smooth(x, window):
    half = window // 2
    pad = np.pad(x, ((half, half), (0, 0)), mode="edge")
    return np.median(np.lib.stride_tricks.sliding_window_view(pad, window, axis=0), axis=-1)


def central_diff(x, ok, s):
    T = len(x)
    t = np.arange(T)
    lo, hi = np.clip(t - s, 0, T - 1), np.clip(t + s, 0, T - 1)
    valid = ok[lo] & ok[hi] & (hi > lo)
    v = (x[hi] - x[lo]) * FPS / np.maximum(hi - lo, 1)
    return np.where(valid, v, 0.0), valid


def ctrl_height(frames: list[list[tuple]]) -> np.ndarray:
    """逐帧操作部框高（每帧面积最大的 ctrl 框 → 插值 → 1 s 中值平滑，下限 0.05）；全程无 ctrl 时用固定参照。"""
    T = len(frames)
    box, ok = np.zeros((T, 4)), np.zeros(T, bool)
    for i, dets in enumerate(frames):
        for c, *b in dets:
            if c != CTRL:
                continue
            b = np.array(b)
            if not ok[i] or b[2] * b[3] > box[i, 2] * box[i, 3]:
                box[i], ok[i] = b, True
    if ok.any():
        c, _ = fill(box, ok)
        c = median_smooth(c, CTRL_SMOOTH)
    else:
        c = np.tile([0.5, 0.6, 0.25, FALLBACK_H], (T, 1))
    return np.maximum(c[:, 3], 0.05)


def hand_rows(frames: list[list[tuple]]):
    """把全部手展开成行：返回 (行所在帧 [N], 行框 [N,4] cx cy w h, 每帧的行号列表)。行序 = 逐帧、帧内文件行序。"""
    t_of, boxes, per_frame = [], [], []
    for t, dets in enumerate(frames):
        rows = []
        for c, *b in dets:
            if c == HAND:
                rows.append(len(boxes))
                t_of.append(t)
                boxes.append(b)
        per_frame.append(rows)
    # 与训练口径一致：手框以 float32 存储后再参与计算
    return np.array(t_of, dtype=int), np.array(boxes, dtype=np.float32).reshape(-1, 4).astype(np.float64), per_frame


def assign_roles(boxes: np.ndarray, per_frame: list[list[int]], p_scope: np.ndarray):
    """逐帧 (送刷手行号, 握镜手行号)，−1 表示缺失。``p_scope``：每行的 p(握镜)。"""
    T = len(per_frame)
    # 逐元素标量计算，与训练口径逐位一致（向量化 log 在个别平台上末位可能不同）
    logit = [float(np.log(max(p, 1e-6) / max(1 - p, 1e-6))) for p in p_scope]
    links = {}
    for t, rows in enumerate(per_frame):
        for r in rows:
            acc = []
            for u in range(max(0, t - SMOOTH_HALF), min(T, t + SMOOTH_HALF + 1)):
                cand = per_frame[u]
                if cand:
                    dist = [np.linalg.norm(boxes[rr, :2] - boxes[r, :2]) for rr in cand]
                    j = int(np.argmin(dist))
                    if dist[j] < LINK_GATE:
                        acc.append(cand[j])
            links[r] = acc
    feed = np.full(T, -1)
    scope = np.full(T, -1)
    for t, rows in enumerate(per_frame):
        if not rows:
            continue
        score = np.array([np.mean([logit[u] for u in links[r]]) for r in rows])
        if len(rows) >= 2:
            order = np.argsort(-score)
            scope[t] = rows[order[0]]
            feed[t] = rows[order[-1]]
        elif score[0] >= 0:
            scope[t] = rows[0]
        else:
            feed[t] = rows[0]
    return feed, scope


def role_columns(boxes: np.ndarray, feed: np.ndarray, scope: np.ndarray, h: np.ndarray) -> dict[str, np.ndarray]:
    """送刷手 / 握镜手是否存在，以及送刷手 4 个跨度的速度和有效标记，共 11 列。"""
    T = len(feed)
    if len(boxes) == 0:  # 全程没有手
        boxes = np.zeros((1, 4))
    fp, sp = feed >= 0, scope >= 0
    fb, _ = fill(np.where(fp[:, None], boxes[np.maximum(feed, 0)], 0.0), fp)
    fb_ok = fill(np.zeros(T), fp, GAP_FILL)[1] if fp.any() else np.zeros(T, bool)
    out = {"feed/present": fp.astype(float), "scope/present": sp.astype(float), "both_roles": (fp & sp).astype(float)}
    for s in SCALES:
        dx, valid = central_diff(fb[:, 0] / h, fb_ok, s)
        dy, _ = central_diff(fb[:, 1] / h, fb_ok, s)
        out[f"dyn/speed_s{s}"] = np.clip(np.hypot(dx, dy), 0, 4)
        out[f"dyn/valid_s{s}"] = valid.astype(float)
    return {k: v.astype(np.float32) for k, v in out.items()}
