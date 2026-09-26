"""按帧 ts 把帧级检测序列降采样到模型契约帧率（在线 Operator 与离线 Segmenter 共用）。

使用方式：
    from app.services.inference.resample import resample_by_ts

    model_frames = resample_by_ts(frames, model_input_fps)

只能降采样：`fps` 高于输入实际帧率时原样保留全部帧，调用方自行保证 `fps` 不超过检测采样率。
"""

from __future__ import annotations

from typing import List, Sequence

from app.domain.detection import FrameDetection


def resample_by_ts(frames: Sequence[FrameDetection], fps: float) -> List[FrameDetection]:
    """相位网格抽稀：从首帧起维护理想采样时刻 next_t，每步 += 1/fps，保留首个 ts ≥ next_t 的帧。

    网格前进（非从"上一保留帧"累加）→ 不累积舍入漂移，2:1 比例下稳定取到目标帧率；遇缺口令网格
    落后当前帧时重锚，避免追补突发。纯 ts 函数——不改帧内容、不合成新 ts。帧数 < 2 原样返回。
    """
    if len(frames) < 2:
        return list(frames)
    min_dt = 1.0 / fps
    kept = [frames[0]]
    next_t = frames[0].ts + min_dt
    for f in frames[1:]:
        if f.ts >= next_t:
            kept.append(f)
            next_t += min_dt
            if next_t <= f.ts:  # 缺口致网格落后：重锚到当前帧，避免追补突发
                next_t = f.ts + min_dt
    return kept
