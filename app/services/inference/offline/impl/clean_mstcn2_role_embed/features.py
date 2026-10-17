"""新基准 mstcn2-role-embed 的 106 维特征：一个视频的逐帧框 + 每只手的手部状态概率 → [T, 106]。

组成（列序见 ``contract.py``）：

| 列 | 数 | 来源 |
|---|---|---|
| bundle 中与排序无关的列 | 63 | ``bundle.py`` |
| 角色序手部槽位列 | 26 | ``slots.py``（28 列中去掉两个槽位的单帧速度） |
| 送刷手 / 握镜手是否存在、送刷手 4 个跨度的速度及有效标记 | 11 | ``roles.py`` |
| 手部状态特征 | 6 | 画面中各只手对 5 类概率的最大值 + 本帧是否有手 |

bundle 中的时间位置列（abs/t_norm、t_sin、t_cos）不使用。
"""

from __future__ import annotations

import numpy as np

from .bundle import bundle_columns
from .contract import FEATURE_COLUMNS
from .hand_state import CLASSES
from .roles import assign_roles, ctrl_height, hand_rows, role_columns
from .slots import SlotContext, role_order


def hand_boxes_per_frame(frames: list[list[tuple]]) -> list[list[tuple]]:
    """每帧手框 [(cx, cy, w, h)]（行序），供裁剪；全部帧展开后的行序即 ``hand_probs`` / 嵌入的行序。"""
    _t, boxes, per_frame = hand_rows(frames)
    return [[tuple(boxes[r]) for r in rows] for rows in per_frame]


def hand_state_columns(t_of: np.ndarray, hand_probs: np.ndarray, T: int) -> dict[str, np.ndarray]:
    out = np.zeros((T, len(CLASSES) + 1), dtype=np.float32)
    for t, prob in zip(t_of, hand_probs):
        out[t, :-1] = np.maximum(out[t, :-1], prob)
        out[t, -1] = 1.0
    names = [f"fine/max_{c}" for c in CLASSES] + ["fine/has_hand"]
    return {n: out[:, i] for i, n in enumerate(names)}


def build_features(frames: list[list[tuple]], hand_probs: np.ndarray, columns=FEATURE_COLUMNS,
                   return_roles: bool = False):
    """[T, 106] float32。``frames``：逐帧检测行；``hand_probs``：[N, 5]，按 ``hand_rows`` 的手行序排列。"""
    t_of, boxes, per_frame = hand_rows(frames)
    T = len(frames)
    if len(hand_probs) != len(boxes):
        raise ValueError(f"手部状态概率 {len(hand_probs)} 行，但检测里有 {len(boxes)} 只手")
    hand_probs = np.asarray(hand_probs, dtype=np.float64).reshape(-1, len(CLASSES))
    feed, scope = assign_roles(boxes, per_frame, hand_probs[:, 0])
    cols = bundle_columns(frames)
    cols.update(SlotContext(frames).hand_columns(role_order(per_frame, feed, scope)))
    cols.update(role_columns(boxes, feed, scope, ctrl_height(frames)))
    cols.update(hand_state_columns(t_of, hand_probs, T))
    missing = [c for c in columns if c not in cols]
    if missing:
        raise ValueError(f"缺少特征列：{missing}")
    x = np.stack([np.asarray(cols[c], dtype=np.float32)[:T] for c in columns], axis=1)
    return (x, {"feed": feed, "scope": scope, "hand_frame": t_of, "hand_boxes": boxes}) if return_roles else x
