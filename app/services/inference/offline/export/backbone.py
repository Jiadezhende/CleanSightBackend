"""视觉 backbone —— 单次前向，同时吐深浅两层特征。

    raw 帧 ──> backbone 前向一次
                 ├── 浅层 stride-8  ──RoIAlign(手框)──> 手部细节（第 4 阶 R2 用）
                 └── 深层 stride-32 ──全局池化───────> 场景上下文（R1 用）

**为什么要两层**：实测手框中位 87×98 px（640×480 画面），在 stride-32 上只占 2.7×3.1 格，
RoIAlign 到 7×7 基本是插值放大；换到 stride-8 是 10.9×12.2 格，细节翻 4 倍，而且更便宜
（截断到浅层 5.6ms vs 跑满 9.8ms）。故不在"全帧 vs 只裁 ROI"里二选一——同一次前向两层都留。

**为什么不是 MobileNet**：同机实测 @640（预热后 20 次均值）——
    YOLO 主干(1.12M)  9.7ms   |  ResNet18(11.7M, channels_last)  21.5ms
    MobileNetV3-Small(2.54M)  95.9ms   |  MobileNetV3-Large(5.5M)  134.7ms
参数相当、FLOPs 少一个数量级的 MobileNetV3 反而慢 9 倍：depthwise separable conv 在 CPU 上
是访存瓶颈、走不到优化过的 GEMM 内核。它的"轻量"是按 FLOPs 与手机 NPU 定义的，在本项目的
CPU 部署形态下不成立。

torch / ultralytics 一律**函数内 import**：本模块可被不消费像素的链路 import 而不触发重依赖。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Tuple

import numpy as np

logger = logging.getLogger(__name__)

# YOLOv8 主干在 model.model 里是纯顺序段（各层 f=-1，无跨层路由）：
#   0 Conv(s2) 1 Conv(s4) 2 C2f 3 Conv(s8) 4 C2f 5 Conv(s16) 6 C2f 7 Conv(s32) 8 C2f 9 SPPF
# 取 [:5] 输出为 stride-8 浅层、[:10] 输出为 stride-32 深层。
_YOLO_SHALLOW_END = 5
_YOLO_DEEP_END = 10
_DEFAULT_YOLO_CKPT = "app/data/clean-large-best.pt"


class Backbone:
    """统一接口：`forward(batch_bgr) -> (deep, shallow)`，两者均为 numpy [n,C,h,w]。"""

    name: str = "none"
    deep_stride: int = 32
    shallow_stride: int = 8

    def forward(self, batch_bgr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        raise NotImplementedError


class _YoloBackbone(Backbone):
    """项目自有 YOLO 的主干（冻结）。特征域与本场景匹配，权重仓库已有、零下载。

    注意其增量语义：bbox 本就是这个 backbone 的产物，故它提供的是"检测头丢掉的信息"
    （纹理、手部姿态、未训练类别的物体），不是一条独立的视觉通道——这正是 R1a/R1b 要对照的。
    """

    def __init__(self, ckpt: str, device: str):
        import torch
        from ultralytics import YOLO

        path = Path(ckpt)
        if not path.exists():
            raise FileNotFoundError(f"YOLO 权重不存在: {path}")
        model = YOLO(str(path)).model.float().eval()
        self._layers = list(model.model)[:_YOLO_DEEP_END]
        self._torch = torch
        self._device = device
        for layer in self._layers:
            layer.to(device)
        self.name = f"yolo:{path.stem}"

    def forward(self, batch_bgr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        torch = self._torch
        # BGR uint8 [n,H,W,3] → RGB float32 [n,3,H,W] / 255（与 ultralytics 预处理同口径）
        x = batch_bgr[..., ::-1].transpose(0, 3, 1, 2).astype(np.float32) / 255.0
        t = torch.from_numpy(np.ascontiguousarray(x)).to(self._device)
        shallow = None
        with torch.no_grad():
            for i, layer in enumerate(self._layers):
                t = layer(t)
                if i == _YOLO_SHALLOW_END - 1:
                    shallow = t
        return t.cpu().numpy(), shallow.cpu().numpy()


class _ResNet18Backbone(Backbone):
    """ImageNet ResNet18：一条**独立**于 bbox 的视觉通道，与 YOLO 主干对照。"""

    def __init__(self, device: str):
        import torch
        from torchvision.models import ResNet18_Weights, resnet18

        self._torch = torch
        self._device = device
        net = resnet18(weights=ResNet18_Weights.IMAGENET1K_V1).eval()
        self._net = net.to(device).to(memory_format=torch.channels_last)
        self.name = "resnet18"
        # ImageNet 归一化常量（channel-first，便于直接广播）
        self._mean = np.array([0.485, 0.456, 0.406], np.float32).reshape(1, 3, 1, 1)
        self._std = np.array([0.229, 0.224, 0.225], np.float32).reshape(1, 3, 1, 1)

    def forward(self, batch_bgr: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
        torch = self._torch
        x = batch_bgr[..., ::-1].transpose(0, 3, 1, 2).astype(np.float32) / 255.0
        x = (x - self._mean) / self._std
        t = torch.from_numpy(np.ascontiguousarray(x)).to(self._device)
        t = t.contiguous(memory_format=torch.channels_last)
        net = self._net
        with torch.no_grad():
            z = net.maxpool(net.relu(net.bn1(net.conv1(t))))
            z = net.layer1(z)          # stride 4
            shallow = net.layer2(z)    # stride 8
            deep = net.layer4(net.layer3(shallow))  # stride 32
        return deep.cpu().numpy(), shallow.cpu().numpy()


def load_backbone(spec: str, device: str = "cpu") -> Backbone:
    """按标识构建 backbone。

    Args:
        spec: `yolo`（用仓库默认 checkpoint）/ `yolo:<路径>` / `resnet18`
        device: cpu / cuda
    """
    spec = (spec or "").strip()
    if spec == "resnet18":
        return _ResNet18Backbone(device)
    if spec == "yolo":
        return _YoloBackbone(_DEFAULT_YOLO_CKPT, device)
    if spec.startswith("yolo:"):
        return _YoloBackbone(spec.split(":", 1)[1], device)
    raise ValueError(f"未知 backbone: {spec!r}（支持 yolo / yolo:<路径> / resnet18）")


def global_pool(feature_map: np.ndarray) -> np.ndarray:
    """[n,C,h,w] → [n,C] 全局平均池化（深层→场景上下文向量）。"""
    return feature_map.mean(axis=(2, 3))
