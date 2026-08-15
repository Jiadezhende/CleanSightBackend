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
from typing import Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)

# 主干边界与深浅层位置一律**探测得出，不硬编码层号**：本仓 checkpoint 是 YOLO11
# （C3k2 块，主干 0..10 以 C2PSA 收尾），与 YOLOv8（C2f 块，主干 0..9 以 SPPF 收尾）
# 层号不同；写死会静默漏掉 C2PSA 整块或切进 head。探测规则见 _probe_backbone：
#   - head 起点 = 第一个 nn.Upsample（v8/v11 的 head 均以它开头）；
#   - 浅层 = 主干内最后一个输出 stride==8 的层；深层 = 主干最后一层。
_SHALLOW_STRIDE = 8
_DEFAULT_YOLO_CKPT = "app/data/clean-large-best.pt"


class Backbone:
    """统一接口：`forward(batch_bgr) -> (deep, shallow)`，两者均为 numpy [n,C,h,w]。

    `forward` 内部**强制分块**：整段直喂会炸内存。实测 143 帧 @640×480 一次前向峰值
    5.0 GB —— 单是 BGR→float32 转换就 527MB（143×3×480×640×4），第一层卷积输出
    937MB（143×16×320×320×4），torch 还要同时持有多个中间层。峰值不随视频长度增长
    （帧源已按段流式），但 5 GB 本身就会在小内存机器或 GPU 上 OOM。
    """

    name: str = "none"
    deep_stride: int = 32
    shallow_stride: int = 8
    # 单次前向的帧数上限。峰值内存 ≈ 正比于它；16 帧实测约 0.6 GB，兼顾吞吐与安全。
    batch_size: int = 16

    def forward(
        self, batch_bgr: np.ndarray, want_shallow: bool = False
    ) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """分块跑 `_forward_chunk` 并拼回，保证峰值内存与输入帧数无关。

        `want_shallow=False`（默认）时**不物化浅层特征图**：它比深层大一个数量级
        （stride-8 是 stride-32 的 16 倍面积，实测 150 帧 369MB vs 46MB），只有 R2 的
        手部 RoIAlign 需要。R1 只用深层全局向量，白搭一份就是纯浪费。
        """
        n = len(batch_bgr)
        if n <= self.batch_size:
            return self._forward_chunk(batch_bgr, want_shallow)
        deeps, shallows = [], []
        for i in range(0, n, self.batch_size):
            d, s = self._forward_chunk(batch_bgr[i: i + self.batch_size], want_shallow)
            deeps.append(d)
            if want_shallow:
                shallows.append(s)
        return np.concatenate(deeps), (np.concatenate(shallows) if want_shallow else None)

    def _forward_chunk(
        self, batch_bgr: np.ndarray, want_shallow: bool
    ) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        """单块前向（帧数 ≤ batch_size），子类实现。"""
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
            # ultralytics 官方权重名（如 yolo11n.pt）允许按需下载，用于「同架构未在本域训练」对照
            if "/" in ckpt or "\\" in ckpt:
                raise FileNotFoundError(f"YOLO 权重不存在: {path}")
            logger.info("[Backbone] 本地无 %s，交由 ultralytics 拉取官方权重", ckpt)
        model = YOLO(str(ckpt)).model.float().eval()
        self._torch = torch
        self._device = device
        self._layers, self._shallow_at = _probe_backbone(model, torch)
        for layer in self._layers:
            layer.to(device)
        self.name = f"yolo:{path.stem}"

    def _forward_chunk(
        self, batch_bgr: np.ndarray, want_shallow: bool
    ) -> Tuple[np.ndarray, Optional[np.ndarray]]:
        torch = self._torch
        # BGR uint8 [n,H,W,3] → RGB float32 [n,3,H,W] / 255（与 ultralytics 预处理同口径）
        x = batch_bgr[..., ::-1].transpose(0, 3, 1, 2).astype(np.float32) / 255.0
        t = torch.from_numpy(np.ascontiguousarray(x)).to(self._device)
        shallow = None
        with torch.no_grad():
            for i, layer in enumerate(self._layers):
                t = layer(t)
                if want_shallow and i == self._shallow_at:
                    shallow = t.cpu().numpy()
        return t.cpu().numpy(), shallow


def _probe_backbone(model, torch) -> Tuple[list, int]:
    """探测主干边界与浅层位置，返回 (主干层列表, 浅层所在下标)。

    不硬编码层号——本仓是 YOLO11（主干 0..10 以 C2PSA 收尾），YOLOv8 是 0..9 以 SPPF 收尾，
    写死会静默漏掉 C2PSA 整块或切进 head。规则：
      1) head 起点 = 第一个 `nn.Upsample`（v8/v11 的 head 均以它开头），其前即主干；
      2) 用 640×640 假输入逐层前向，记录各层输出 stride；
      3) 浅层取主干内**最后一个** stride==8 的层（该尺度语义最成熟），深层即主干末层。
    """
    import torch.nn as nn

    layers = list(model.model)
    head_at = next(
        (i for i, layer in enumerate(layers) if isinstance(layer, nn.Upsample)), len(layers)
    )
    backbone = layers[:head_at]
    if not backbone:
        raise ValueError("未能识别 YOLO 主干（没找到 head 起点）")

    probe = torch.zeros(1, 3, 640, 640)
    shallow_at = -1
    with torch.no_grad():
        t = probe
        for i, layer in enumerate(backbone):
            t = layer(t)
            if 640 // t.shape[-1] == _SHALLOW_STRIDE:
                shallow_at = i
    if shallow_at < 0:
        raise ValueError(f"主干内未找到 stride-{_SHALLOW_STRIDE} 层")
    logger.info(
        "[Backbone] 主干 %d 层（末层 %s，stride %d）；浅层取第 %d 层 %s",
        len(backbone), type(backbone[-1]).__name__, 640 // t.shape[-1],
        shallow_at, type(backbone[shallow_at]).__name__,
    )
    return backbone, shallow_at


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

    def _forward_chunk(
        self, batch_bgr: np.ndarray, want_shallow: bool
    ) -> Tuple[np.ndarray, Optional[np.ndarray]]:
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
        return deep.cpu().numpy(), (shallow.cpu().numpy() if want_shallow else None)


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
