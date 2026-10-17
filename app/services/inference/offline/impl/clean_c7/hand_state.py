"""手部状态分类器：每只手的图像 → 5 类概率（scope / long_brush / short_brush_hat / syringe / none）。

    net = load_dino(backbone_dir, device)
    crop = square_crop(mask_overlay(frame), box)          # frame：640×480 BGR；box：归一化 cx cy w h
    emb = embed_crops(net, crops, device)                  # [N, 768] float16
    probs = HandStateClassifier(npz).probs(emb.astype(np.float64))

裁剪 / 嵌入口径同仓库 ``tools/hand_hold_v41.py embed-all``：时间戳水印涂灰 → 1.25 倍正方形裁剪（越界填灰）→ 224 →
DINOv2 ViT-S/14 取 [CLS ‖ patch 均值]，GPU 上 fp16 autocast，float16 存储。p(握镜) = 概率第 0 列。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Iterator, Optional

import numpy as np

CLASSES = ("scope", "long_brush", "short_brush_hat", "syringe", "none")
CROP_SCALE, CROP_SIZE = 1.25, 224
GRAY = (124, 116, 104)  # BGR 下的 ImageNet 均值色，用于裁剪越界填充与水印遮挡
OVERLAY = (0, 48, 0, 300)  # y0, y1, x0, x1：640×480 画面左上角的时间戳水印
DINO = "dinov2_vits14"
DINO_WEIGHTS = "dinov2_vits14_pretrain.pth"  # 骨干目录 = torch.hub 的 dinov2 代码目录 + 这份权重
EMBED_DIM = 768
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


class HandStateClassifier:
    def __init__(self, npz_path: Path):
        z = np.load(npz_path, allow_pickle=False)
        self.mu, self.basis = z["mu"], z["basis"]
        self.means, self.stds, self.betas, self.valid = z["means"], z["stds"], z["betas"], z["valid"]
        self.classes = tuple(str(c) for c in z["classes"])
        if self.classes != CLASSES:
            raise ValueError(f"{npz_path}: 类别 {self.classes} 与 {CLASSES} 不一致")

    def logits(self, emb: np.ndarray) -> np.ndarray:
        """[N, 768] → [N, 5]；没有足够人工样本的类恒为 −30。"""
        z = (np.asarray(emb, dtype=np.float64) - self.mu) @ self.basis
        cols = []
        for k in range(len(self.classes)):
            if not self.valid[k]:
                cols.append(np.full(len(z), -30.0))
                continue
            design = np.hstack([np.ones((z.shape[0], 1)), (z - self.means[k]) / self.stds[k]])
            cols.append(design @ self.betas[k])
        return np.stack(cols, axis=1)

    def probs(self, emb: np.ndarray) -> np.ndarray:
        g = self.logits(emb)
        g = np.exp(g - g.max(axis=1, keepdims=True))
        return g / g.sum(axis=1, keepdims=True)


def mask_overlay(frame: np.ndarray) -> np.ndarray:
    """时间戳水印涂灰，返回副本（解码帧可能只读）。"""
    out = frame.copy()
    y0, y1, x0, x1 = OVERLAY
    out[y0:y1, x0:x1] = GRAY
    return out


def square_crop(frame, box, scale=CROP_SCALE, size=CROP_SIZE):
    import cv2

    height, width = frame.shape[:2]
    cx, cy, w, h = box
    side = max(int(round(scale * max(w * width, h * height))), 32)
    x0, y0 = int(round(cx * width - side / 2)), int(round(cy * height - side / 2))
    padded = cv2.copyMakeBorder(frame, side, side, side, side, cv2.BORDER_CONSTANT, value=GRAY)
    crop = padded[y0 + side: y0 + 2 * side, x0 + side: x0 + 2 * side]
    return cv2.resize(crop, (size, size), interpolation=cv2.INTER_AREA if side > size else cv2.INTER_CUBIC)


def load_dino(backbone_dir: Path, device: str):
    """从本地骨干目录建 DINOv2（不联网、不读 torch.hub 缓存），strict 载权重。"""
    import torch

    backbone_dir = Path(backbone_dir)
    net = torch.hub.load(str(backbone_dir), DINO, source="local", pretrained=False)
    state = torch.load(backbone_dir / DINO_WEIGHTS, map_location="cpu", weights_only=True)
    net.load_state_dict(state, strict=True)
    return net.eval().to(device)


def _batches(crops: Iterable[np.ndarray], batch_size: int) -> Iterator[list[np.ndarray]]:
    batch = []
    for crop in crops:
        batch.append(crop)
        if len(batch) == batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def embed_crops(net, crops: Iterable[np.ndarray], device: str, batch_size: int = 16,
                max_crops_per_s: Optional[float] = None) -> np.ndarray:
    """逐批嵌入 [N, 768] float16；``max_crops_per_s`` 给定时按平均速率限速（与在线推理共卡时让出 GPU）。"""
    import cv2
    import torch

    out, done, start = [], 0, time.monotonic()
    with torch.no_grad():
        for batch in _batches(crops, batch_size):
            x = np.stack([cv2.cvtColor(im, cv2.COLOR_BGR2RGB) for im in batch])
            x = torch.from_numpy((x.astype(np.float32) / 255.0 - MEAN) / STD).permute(0, 3, 1, 2).contiguous()
            with torch.autocast(device_type="cuda", dtype=torch.float16, enabled=str(device).startswith("cuda")):
                feats = net.forward_features(x.to(device))
            out.append(torch.cat([feats["x_norm_clstoken"], feats["x_norm_patchtokens"].mean(dim=1)], dim=1)
                       .float().cpu())
            done += len(batch)
            if max_crops_per_s:
                time.sleep(max(0.0, done / max_crops_per_s - (time.monotonic() - start)))
    if not out:
        return np.zeros((0, EMBED_DIM), dtype=np.float16)
    return torch.cat(out).numpy().astype(np.float16)
