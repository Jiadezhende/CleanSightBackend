"""按帧 ts 把帧级检测序列降采样到模型契约帧率（在线 Operator 与离线 Segmenter 共用）。

使用方式：
    from app.services.inference.resample import resample_by_ts

    model_frames = resample_by_ts(frames, model_input_fps)               # 宽松：输入慢于 fps 时原样放行
    model_frames = resample_by_ts(frames, model_input_fps, strict=True)  # 严格：输入慢于 fps 即 ValueError

只能降采样，不合成帧。
"""

from __future__ import annotations

from statistics import median
from typing import List, Sequence

from app.domain.detection import FrameDetection

_RATE_SLACK = 0.01  # strict 下限的相对余量，吸收 ts 舍入与抖动


def resample_by_ts(frames: Sequence[FrameDetection], fps: float, *, strict: bool = False) -> List[FrameDetection]:
    """相位网格抽稀：从首帧起维护理想采样时刻 next_t，每步 += 1/fps，保留首个 ts ≥ next_t - tol 的帧。

    tol = 半个输入帧间隔（相邻 ts 差的中位数）：整数比下网格点与帧 ts 重合，ts 舍入 / 抖动不致跳到下一帧。
    网格前进（非从"上一保留帧"累加）→ 不累积漂移；遇缺口令网格落后当前帧时重锚，避免追补突发。
    纯 ts 函数——不改帧内容、不合成新 ts。帧数 < 2 原样返回。
    strict=True 时输入帧率低于 fps（超出 1% 余量）抛 ValueError。
    """
    if len(frames) < 2:
        return list(frames)
    dts = [b.ts - a.ts for a, b in zip(frames, frames[1:]) if b.ts > a.ts]
    if not dts:
        return list(frames)
    input_dt = median(dts)
    min_dt = 1.0 / fps
    if strict and input_dt > min_dt * (1 + _RATE_SLACK):
        raise ValueError(f"输入帧率 {1.0 / input_dt:.3f}fps 低于契约帧率 {fps}fps，只能降采样")
    tol = input_dt / 2
    kept = [frames[0]]
    next_t = frames[0].ts + min_dt
    for f in frames[1:]:
        if f.ts >= next_t - tol:
            kept.append(f)
            next_t += min_dt
            if next_t - tol <= f.ts:  # 缺口致网格落后：重锚到当前帧，避免追补突发
                next_t = f.ts + min_dt
    return kept
