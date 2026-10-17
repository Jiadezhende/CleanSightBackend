"""clean_bbox_v3 的槽位通道投影：把 v2 插值特征 ``[T,8]`` 投到器械轴坐标系（along / across / 面积对数比）。

bundle 只用这一个函数与它的裁剪常量；轴本身由 ``clean_bbox_v5.scope_frame_offline``（ctrl→mid）给出。
训练仓 v3 的整段 113 维矩阵与它自己的 control→distal 轴在 bundle 里全被覆盖或丢弃，已删。
"""

from __future__ import annotations

import numpy as np

_CLIP_POS = 4.0
_CLIP_LOG = 6.0
_FALLBACK_REF_LEN = 0.35
_FALLBACK_REF_AREA = 0.02

V3_SLOT_CHANNELS = [
    "present",
    "conf",
    "along",
    "across",
    "log_area_ratio",
    "speed",
    "missing_age",
    "imputed",
]


def _slot_channels(feature: np.ndarray, active: np.ndarray, axes, ref_len, ref_area, control):
    """把 v2 插值特征 ``[T,8]`` 转成 v3 的 8 通道 scope 坐标特征。"""
    present = feature[:, 0]
    confidence = feature[:, 1]
    centers = feature[:, 2:4]
    area = feature[:, 4]
    speed = feature[:, 5]
    missing_age = feature[:, 6]
    imputed = feature[:, 7]

    relative = centers - control
    along = np.einsum("td,td->t", relative, axes) / ref_len
    perpendicular = np.stack([-axes[:, 1], axes[:, 0]], axis=1)
    across = np.einsum("td,td->t", relative, perpendicular) / ref_len
    log_ratio = np.log((area + 1e-4) / (ref_area + 1e-4))

    channels = np.stack(
        [
            present,
            confidence,
            np.clip(along, -_CLIP_POS, _CLIP_POS),
            np.clip(across, -_CLIP_POS, _CLIP_POS),
            np.clip(log_ratio, -_CLIP_LOG, _CLIP_LOG),
            speed,
            missing_age,
            imputed,
        ],
        axis=1,
    ).astype(np.float32)
    # 与 v2 一致：缺席帧把连续/置信度通道清零，只保留 present/missing_age/imputed
    channels[~active, 1:6] = 0.0
    return channels
