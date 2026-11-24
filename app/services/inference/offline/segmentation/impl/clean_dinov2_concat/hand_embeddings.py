"""按训练侧 hand_hold_v41.py 的口径裁剪和编码手部图像。"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Iterable, Iterator, Optional

import numpy as np

from .contract import EMBED_DIM

CROP_SCALE, CROP_SIZE = 1.25, 224
GRAY = (124, 116, 104)  # BGR 下的 ImageNet 均值色，用于裁剪越界填充与水印遮挡
OVERLAY = (0, 48, 0, 300)  # y0, y1, x0, x1：640×480 画面左上角的时间戳水印
DINO = "dinov2_vits14"
DINO_WEIGHTS = "dinov2_vits14_pretrain.pth"  # 骨干目录 = torch.hub 的 dinov2 代码目录 + 这份权重
MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


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


def backbone_key(backbone_dir: Path) -> str:
    """嵌入的来源标识：骨干名 + 权重 sha256 + 裁剪口径。任何一项变了，旧嵌入缓存即失效。"""
    import hashlib

    digest = hashlib.sha256()
    with open(Path(backbone_dir) / DINO_WEIGHTS, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            digest.update(chunk)
    return f"{DINO}|{digest.hexdigest()}|crop{CROP_SCALE}x{CROP_SIZE}|overlay{OVERLAY}"


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
