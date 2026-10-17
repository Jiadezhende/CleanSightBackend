"""角色序手部槽位：槽位 1 = 握镜手，槽位 2 = 送刷手，按此顺序重算 bundle 中与手部槽位有关的 28 列。

原 bundle 把两只手按框面积排序（面积序）。这里只改变「哪只手放进哪个槽位」，各列的计算方式不变：
短缺失插值、器械轴坐标、手到器械的距离都沿用 ``bundle.py`` 的实现。
与仓库 ``tools/hand_slot_stability.py::Video.hand_columns`` + ``tools/build_feedrole_slot_arm.py`` 同口径。
"""

from __future__ import annotations

import numpy as np

from .bundle import Bundle, _impute_short_gaps, _pair_block, _slot_channels
from .frames import FPS, HAND

ANCHORS = ("scope_control_body", "scope_mid_section")


def _clip01(v: float) -> float:
    return min(max(v, 0.0), 1.0)


def _cxcya(det) -> tuple:
    """(cx, cy, area)，数值裁剪与 float32 化同 bundle 的读框口径。"""
    _c, cx, cy, w, h = det
    return tuple(np.float32(v) for v in (_clip01(cx), _clip01(cy), _clip01(w) * _clip01(h)))


class SlotContext:
    """一个视频与排序无关的部分：每帧的手框、操作部 / 插入部锚点和器械轴。"""

    def __init__(self, frames: list[list[tuple]], bundle: Bundle):
        """``bundle``：同一视频 ``bundle.compute_bundle`` 的结果，复用它的锚点轨迹与器械轴（与 bundle 列同一口径）。"""
        self.T = len(frames)
        self.hands = [[_cxcya(d) for d in dets if d[0] == HAND] for dets in frames]
        self.feats = {name: bundle.features[name] for name in ANCHORS}
        self.active = {name: bundle.active[name] for name in ANCHORS}
        self.frame = bundle.frame

    def hand_columns(self, order) -> dict[str, np.ndarray]:
        """order[t] = (槽位 1 的手在本帧中的序号或 None, 槽位 2 的 …) → 受排序影响的 28 列。"""
        feats, active = dict(self.feats), dict(self.active)
        cols = {}
        for j in (1, 2):
            raw = np.zeros((self.T, 5), dtype=np.float32)
            for t, pair in enumerate(order):
                k = pair[j - 1]
                if k is not None:
                    c = self.hands[t][k]
                    raw[t] = (1.0, c[0], c[1], c[2], 1.0)
            key = f"hand_top{j}"
            feats[key], active[key] = _impute_short_gaps(raw, FPS)
            for c, name in enumerate(("present", "conf", "cx", "cy", "area", "speed", "missing_age", "imputed")):
                cols[f"abs/{key}_{name}"] = feats[key][:, c]
            ch = _slot_channels(feats[key], active[key], self.frame.axes, self.frame.ref_len, self.frame.ref_area,
                                self.frame.origin)
            for c, name in ((2, "along"), (3, "across"), (4, "log_area_ratio")):
                cols[f"scope/{key}_{name}"] = ch[:, c]
        pairs, names = _pair_block(feats, active, self.T)
        for c, name in enumerate(names):
            if name.startswith("pair/hand_"):
                cols[name] = pairs[:, c]
        return cols


def role_order(per_frame_rows: list[list[int]], feed: np.ndarray, scope: np.ndarray):
    """角色判定（全局行号）→ 每帧 (握镜手的帧内序号, 送刷手的帧内序号)。"""
    order = []
    for t, rows in enumerate(per_frame_rows):
        a = rows.index(scope[t]) if scope[t] >= 0 else None
        b = rows.index(feed[t]) if feed[t] >= 0 else None
        if a is not None and a == b:
            b = None
        order.append((a, b))
    return order
