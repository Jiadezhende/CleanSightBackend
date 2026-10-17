"""CLEAN 新基准 mstcn2-role-embed 离线策略（YAML ``offline.class`` 指向 ``CleanMSTCN2RoleEmbedSegmenter``）。

    offline:
      class: app.services.inference.offline.impl.clean_mstcn2_role_embed.segmenter.CleanMSTCN2RoleEmbedSegmenter
      params:
        model_dir: ${CLEANSIGHT_MODEL_PATH:./app/data}/clean-offline-mstcn2-role-embed
        backbone_dir: ${CLEANSIGHT_MODEL_PATH:./app/data}/backbones/dinov2_vits14
        device: cuda
        max_crops_per_s: 150
        max_duration_s: 900

物料目录内文件名固定：``model_dir/{temporal.pt, hand_state.npz}``，``backbone_dir`` = dinov2 hub 代码 + 权重。
输入契约：训练帧率 15 fps（快于它按 ts 抽稀；慢于它照常跑，只告警）、640×480；
只看前 ``max_duration_s``（从首帧起），超出部分截断不推理；raw 录像缺任何一帧有手的画面即整次失败，不跳手。
手部嵌入缓存在本 run 的 ``inference/cache_hand_embeddings.npz``：手（ts、框）与骨干口径都一致才复用，否则重算覆盖。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional, Sequence

import numpy as np

from app.types.detection import FrameDetection
from app.types.temporal import LabelProbs, TemporalSegment
from app.services.inference.offline.segmenter import OfflineContext, OfflineSegmenter
from app.services.inference.resample import input_fps, resample_by_ts

from .contract import ACTION_NAMES, HAND_STATE_FILE, HEIGHT, TEMPORAL_FILE, WIDTH
from .features import build_features, hand_boxes_per_frame
from .frames import FPS, detection_rows
from .hand_state import (
    DINO_WEIGHTS, EMBED_DIM, HandStateClassifier, backbone_key, embed_crops, load_dino, mask_overlay, square_crop,
)
from .postprocess import PP_MIN_LEN, merge_short_segments

logger = logging.getLogger(__name__)

CACHE_NAME = "hand_embeddings"  # 键：ts [N] / boxes [N,4] / emb [N,768] fp16 / backbone 标识
_RATE_SLACK = 0.01  # 帧率比较的相对余量，吸收 ts 舍入与抖动（同 resample）


@dataclass(frozen=True)
class RoleEmbedInput:
    ts: np.ndarray        # [T] float64，帧捕获 ts
    features: np.ndarray  # [T, 106] float32


class CleanMSTCN2RoleEmbedSegmenter(OfflineSegmenter):
    """手部角色化 MS-TCN2：手部裁剪 → DINOv2 → 手部状态分类器 → 106 维特征 → 整段前向 → pp20。"""

    uses_context = True
    labels = ACTION_NAMES

    def __init__(
        self,
        model_dir: str,
        backbone_dir: str,
        *,
        device: str = "cuda",
        max_crops_per_s: Optional[float] = 150.0,
        batch_size: int = 16,
        min_segment_frames: int = PP_MIN_LEN,
        max_duration_s: Optional[float] = 900.0,
    ):
        if not (device == "cpu" or device.startswith("cuda")):
            raise ValueError(f"device 只能是 cpu / cuda[:n]: {device!r}")
        if max_crops_per_s is not None and float(max_crops_per_s) <= 0:
            raise ValueError(f"max_crops_per_s 必须大于 0（不限速填 null）: {max_crops_per_s}")
        if int(batch_size) < 1:
            raise ValueError(f"batch_size 必须 >= 1: {batch_size}")
        if int(min_segment_frames) < 0:
            raise ValueError(f"min_segment_frames 不能为负: {min_segment_frames}")
        if max_duration_s is not None and float(max_duration_s) <= 0:
            raise ValueError(f"max_duration_s 必须大于 0（不截断填 null）: {max_duration_s}")
        self.model_dir = Path(model_dir)
        self.backbone_dir = Path(backbone_dir)
        self.device = device
        self.max_crops_per_s = None if max_crops_per_s is None else float(max_crops_per_s)
        self.batch_size = int(batch_size)
        self.min_segment_frames = int(min_segment_frames)
        self.max_duration_s = None if max_duration_s is None else float(max_duration_s)
        self._last_probs: Optional[LabelProbs] = None

    def preprocess(self, frames: Sequence[FrameDetection], context: OfflineContext) -> RoleEmbedInput:
        frames = _truncate(frames, self.max_duration_s)
        _warn_low_rate(frames)
        frames = _dedup_ts(resample_by_ts(frames, FPS))
        _check_resolution(frames)
        ts = np.asarray([f.ts for f in frames], dtype=np.float64)
        rows = detection_rows(frames, width=WIDTH, height=HEIGHT)
        emb = self._embed(ts, hand_boxes_per_frame(rows), context)
        hand_probs = HandStateClassifier(self.model_dir / HAND_STATE_FILE).probs(emb.astype(np.float64))
        return RoleEmbedInput(ts=ts, features=build_features(rows, hand_probs))

    def segment(self, model_input: RoleEmbedInput) -> List[TemporalSegment]:
        from .model import load_model, predict_probs  # torch 只在真正前向时拉起

        if len(model_input.ts) == 0:
            return []
        model = load_model(self.model_dir / TEMPORAL_FILE)  # 整段前向 CPU 即可（9000 帧 ~16 ms）
        probs = predict_probs([model], model_input.features)
        labels = merge_short_segments(probs.argmax(axis=1), self.min_segment_frames)
        self._last_probs = LabelProbs(ts=model_input.ts, probs=probs, labels=self.labels)
        return self._segments(model_input.ts, labels, probs)

    def materials(self) -> Sequence[Path]:
        return (
            self.model_dir / TEMPORAL_FILE,
            self.model_dir / HAND_STATE_FILE,
            self.backbone_dir / "hubconf.py",
            self.backbone_dir / DINO_WEIGHTS,
        )

    def label_probs(self) -> Optional[LabelProbs]:
        return self._last_probs

    def _segments(self, ts: np.ndarray, labels: np.ndarray, probs: np.ndarray) -> List[TemporalSegment]:
        """连续同标签帧 → 一段（跳过 idle）；conf = 段内该标签概率的均值。"""
        bounds = np.flatnonzero(np.diff(labels)) + 1
        starts = np.concatenate([[0], bounds])
        ends = np.concatenate([bounds, [labels.size]])
        out = []
        for s, e in zip(starts, ends):
            label = int(labels[s])
            if label == 0:
                continue
            out.append(TemporalSegment(
                producer=self.name,
                label=self.labels[label],
                start=float(ts[s]),
                end=float(ts[e - 1]),
                conf=min(1.0, max(0.0, float(probs[s:e, label].mean()))),
                meta={"model": self.model_dir.name},
            ))
        return out

    def _embed(self, ts: np.ndarray, hands: List[List[tuple]], context: OfflineContext) -> np.ndarray:
        """每只手的嵌入，行序 = 逐帧、帧内检测行序（= ``hand_rows``）；本 run 的缓存与这些手逐行一致就直接用。"""
        row_ts = np.asarray([t for t, boxes in zip(ts, hands) for _ in boxes], dtype=np.float64)
        row_boxes = np.asarray([b for boxes in hands for b in boxes], dtype=np.float64).reshape(-1, 4)
        if len(row_ts) == 0:
            return np.zeros((0, EMBED_DIM), dtype=np.float16)
        key = backbone_key(self.backbone_dir)
        cached = context.load_cache(CACHE_NAME)
        if _cache_matches(cached, row_ts, row_boxes, key):
            logger.info("[CleanMSTCN2RoleEmbedSegmenter] 手部嵌入命中缓存 | hands=%d", len(row_ts))
            return cached["emb"]
        emb = self._compute_embeddings(ts, hands, context)
        context.save_cache(CACHE_NAME, {"ts": row_ts, "boxes": row_boxes, "emb": emb, "backbone": np.array(key)})
        return emb

    def _compute_embeddings(self, ts: np.ndarray, hands: List[List[tuple]], context: OfflineContext) -> np.ndarray:
        """按 ts 从 raw 轨取有手的帧，逐只手裁剪嵌入；缺任何一帧即失败，不跳手。"""
        wanted = {float(t): boxes for t, boxes in zip(ts, hands) if boxes}
        total = sum(len(b) for b in wanted.values())
        device = self._resolve_device()
        started = time.monotonic()
        net = load_dino(self.backbone_dir, device)
        found = set()

        def crops():
            for frame in context.video(width=WIDTH, height=HEIGHT, start_ts=min(wanted), end_ts=max(wanted)):
                boxes = wanted.get(frame.timestamp)
                if boxes is None or frame.timestamp in found:
                    continue
                found.add(frame.timestamp)
                image = mask_overlay(frame.frame)
                for box in boxes:
                    yield square_crop(image, np.array(box))

        emb = embed_crops(net, crops(), device, self.batch_size, self.max_crops_per_s)
        if len(found) != len(wanted):
            missing = sorted(set(wanted) - found)
            raise ValueError(f"raw 录像缺 {len(missing)}/{len(wanted)} 帧有手的画面（如 ts={missing[:3]}），不跳手")
        if len(emb) != total:
            raise AssertionError(f"手部嵌入 {len(emb)} 行，应为 {total}")
        logger.info(
            "[CleanMSTCN2RoleEmbedSegmenter] 手部嵌入完成 | frames=%d | hands=%d | device=%s | %.1fs",
            len(wanted), total, device, time.monotonic() - started,
        )
        return emb

    def _resolve_device(self) -> str:
        import torch

        if self.device.startswith("cuda") and not torch.cuda.is_available():
            raise RuntimeError(
                f"device={self.device} 但本进程看不到 GPU（CUDA_VISIBLE_DEVICES="
                f"{os.environ.get('CUDA_VISIBLE_DEVICES')!r}）；无 GPU 的机器把 device 配成 cpu"
            )
        return self.device


def _cache_matches(cached, row_ts: np.ndarray, row_boxes: np.ndarray, key: str) -> bool:
    """缓存与本次的手逐行一致（ts、框）且来自同一骨干与裁剪口径。"""
    if not cached or not {"ts", "boxes", "emb", "backbone"} <= cached.keys():
        return False
    return (
        str(cached["backbone"]) == key
        and cached["emb"].shape == (len(row_ts), EMBED_DIM)
        and np.array_equal(cached["ts"], row_ts)
        and np.array_equal(cached["boxes"], row_boxes)
    )


def _truncate(frames: Sequence[FrameDetection], max_duration_s: Optional[float]) -> Sequence[FrameDetection]:
    """只留首帧起 ``max_duration_s`` 内的帧（闭区间），限住超长 step 的取帧 / 嵌入开销。"""
    if max_duration_s is None or not frames or frames[-1].ts - frames[0].ts <= max_duration_s:
        return frames
    cutoff = frames[0].ts + max_duration_s
    kept = [f for f in frames if f.ts <= cutoff]
    logger.warning(
        "[CleanMSTCN2RoleEmbedSegmenter] 检测时长 %.0fs 超过上限 %.0fs，只推理前 %.0fs | frames %d → %d",
        frames[-1].ts - frames[0].ts, max_duration_s, max_duration_s, len(frames), len(kept),
    )
    return kept


def _dedup_ts(frames: Sequence[FrameDetection]) -> Sequence[FrameDetection]:
    """同 ts 只留首帧：取帧与嵌入按 ts 对齐，同 ts 多帧会让嵌入行数与手行数对不上（输入慢于 7.5fps 时 resample 放行重复 ts）。"""
    seen = set()
    kept = [f for f in frames if not (f.ts in seen or seen.add(f.ts))]
    if len(kept) == len(frames):
        return frames
    logger.warning("[CleanMSTCN2RoleEmbedSegmenter] 检测有重复 ts，同 ts 只留首帧 | frames %d → %d", len(frames), len(kept))
    return kept


def _warn_low_rate(frames: Sequence[FrameDetection]) -> None:
    """检测帧率随在线负载波动：低于训练帧率只告警、不失败（按帧计的窗口与速度会相应拉长）。"""
    rate = input_fps(frames)
    if rate is None or rate >= FPS * (1 - _RATE_SLACK):
        return
    logger.warning("[CleanMSTCN2RoleEmbedSegmenter] 输入帧率 %.3ffps 低于训练帧率 %.1ffps，照常推理", rate, FPS)


def _check_resolution(frames: Sequence[FrameDetection]) -> None:
    for f in frames:
        if (f.frame_width, f.frame_height) not in ((None, None), (WIDTH, HEIGHT)):
            raise ValueError(f"mstcn2-role-embed 只接受 {WIDTH}×{HEIGHT} 输入（训练分辨率 / 水印位置），得到 {f.frame_width}×{f.frame_height}")
