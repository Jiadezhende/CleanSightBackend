"""离线分割的帧准备与输出校验。"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Literal, Sequence

import numpy as np

from app.services.inference.resample import input_fps, resample_by_ts
from app.types.detection import FrameDetection
from app.types.temporal import LabelProbs, TemporalSegment

logger = logging.getLogger(__name__)

_RATE_SLACK = 0.01  # 延用离线低帧率告警的相对余量


@dataclass(frozen=True)
class FrameInputContract:
    """目标帧率、低帧率行为、时间戳唯一性和固定尺寸。"""

    target_fps: float | None = None
    low_rate: Literal["warn", "error"] = "warn"
    unique_ts: bool = False
    expected_size: tuple[int, int] | None = None


def prepare_frames(
    frames: Sequence[FrameDetection],
    contract: FrameInputContract,
    *,
    max_duration_s: float | None = None,
    producer: str,
) -> Sequence[FrameDetection]:
    """依次截断、抽稀、去重和校验尺寸。保留原始帧及其时间戳。"""
    if not frames:
        return frames

    duration = frames[-1].ts - frames[0].ts
    if max_duration_s is not None and duration > max_duration_s:
        cutoff = frames[0].ts + max_duration_s
        kept = [frame for frame in frames if frame.ts <= cutoff]
        logger.warning(
            "[%s] 检测时长 %.0fs 超过上限 %.0fs，只推理前 %.0fs | frames %d → %d",
            producer, duration, max_duration_s, max_duration_s, len(frames), len(kept),
        )
        frames = kept

    if contract.target_fps is not None:
        strict = contract.low_rate == "error"
        if not strict:
            rate = input_fps(frames)
            if rate is not None and rate < contract.target_fps * (1 - _RATE_SLACK):
                logger.warning(
                    "[%s] 输入帧率 %.3ffps 低于训练帧率 %.1ffps，照常推理",
                    producer, rate, contract.target_fps,
                )
        frames = resample_by_ts(frames, contract.target_fps, strict=strict)

    if contract.unique_ts:
        kept = []
        for frame in frames:
            if not kept or frame.ts != kept[-1].ts:
                kept.append(frame)
        if len(kept) != len(frames):
            logger.warning(
                "[%s] 检测有重复 ts，同 ts 只留首帧 | frames %d → %d",
                producer, len(frames), len(kept),
            )
            frames = kept

    if contract.expected_size is not None:
        width, height = contract.expected_size
        for frame in frames:
            if (frame.frame_width, frame.frame_height) not in ((None, None), contract.expected_size):
                raise ValueError(
                    f"{producer} 只接受 {width}×{height} 输入，得到 {frame.frame_width}×{frame.frame_height}"
                )
    return frames


def validate_label_probs_shape(label_probs: LabelProbs) -> None:
    """检查逐帧概率矩阵与时间戳和类别表是否对齐。"""
    ts = np.asarray(label_probs.ts)
    probs = np.asarray(label_probs.probs)
    if ts.ndim != 1 or probs.ndim != 2:
        raise ValueError(f"期望 ts [T] 与 probs [T,C]，得到 ts{ts.shape} probs{probs.shape}")
    if ts.shape[0] != probs.shape[0]:
        raise ValueError(f"ts 与 probs 行数不等：{ts.shape[0]} != {probs.shape[0]}")
    if len(label_probs.labels) != probs.shape[1]:
        raise ValueError(f"labels 数与 probs 列数不等：{len(label_probs.labels)} != {probs.shape[1]}")


def validate_segments(segments: Sequence[TemporalSegment], *, producer: str) -> None:
    """校验所有动作段及其所属策略。"""
    for segment in segments:
        if not isinstance(segment, TemporalSegment):
            raise ValueError(f"segmenter 产出非 TemporalSegment: {type(segment).__name__}")
        if segment.producer != producer:
            raise ValueError(
                f"TemporalSegment.producer '{segment.producer}' != segmenter name '{producer}'"
            )
        if not (math.isfinite(segment.start) and math.isfinite(segment.end)):
            raise ValueError(f"TemporalSegment 时间非有限数: start={segment.start} end={segment.end}")
        if segment.start > segment.end:
            raise ValueError(f"TemporalSegment start > end: {segment.start} > {segment.end}")
        if not (0.0 <= segment.conf <= 1.0):
            raise ValueError(f"TemporalSegment conf 越界: {segment.conf}")
