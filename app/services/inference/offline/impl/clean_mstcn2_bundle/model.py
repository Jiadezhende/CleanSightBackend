"""时序模型 MS-TCN2（推理部分），与仓库 ``framework/cleansight_eval/temporal/models/mstcn2.py`` 结构一致。

输入 [B, T, 94] → 模型内 z-score（均值 / 方差是随权重保存的 buffer）→ 预测生成 stage → 精化 stage →
最后一个 stage 的 logits [B, T, 6]。双向卷积，非因果，只能整段离线推理。结构超参写死在 ``contract.STRUCTURE``，
权重是训练状态 checkpoint，只取 ``model_state``、strict 加载。
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .contract import STRUCTURE


class DilatedResidualLayer(nn.Module):
    def __init__(self, dilation: int, channels: int, dropout: float):
        super().__init__()
        self.conv_dilated = nn.Conv1d(channels, channels, kernel_size=3, padding=dilation, dilation=dilation)
        self.conv_1x1 = nn.Conv1d(channels, channels, kernel_size=1)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        return x + self.dropout(self.conv_1x1(F.relu(self.conv_dilated(x))))


class DualDilatedResidualLayer(nn.Module):
    def __init__(self, i: int, num_layers: int, channels: int, dropout: float):
        super().__init__()
        d1, d2 = 2 ** i, 2 ** (num_layers - 1 - i)
        self.conv_fine = nn.Conv1d(channels, channels, kernel_size=3, padding=d1, dilation=d1)
        self.conv_coarse = nn.Conv1d(channels, channels, kernel_size=3, padding=d2, dilation=d2)
        self.conv_1x1 = nn.Conv1d(2 * channels, channels, kernel_size=1)
        self.dropout = nn.Dropout(dropout)

    def forward(self, x):
        out = F.relu(torch.cat([self.conv_fine(x), self.conv_coarse(x)], dim=1))
        return x + self.dropout(self.conv_1x1(out))


class PredictionGeneration(nn.Module):
    def __init__(self, in_dim, num_classes, channels, num_layers, dropout):
        super().__init__()
        self.in_proj = nn.Conv1d(in_dim, channels, kernel_size=1)
        self.layers = nn.ModuleList(DualDilatedResidualLayer(i, num_layers, channels, dropout) for i in range(num_layers))
        self.out = nn.Conv1d(channels, num_classes, kernel_size=1)

    def forward(self, x):
        z = self.in_proj(x)
        for layer in self.layers:
            z = layer(z)
        return self.out(z)


class Refinement(nn.Module):
    def __init__(self, num_classes, channels, num_layers, dropout):
        super().__init__()
        self.in_proj = nn.Conv1d(num_classes, channels, kernel_size=1)
        self.layers = nn.ModuleList(DilatedResidualLayer(2 ** i, channels, dropout) for i in range(num_layers))
        self.out = nn.Conv1d(channels, num_classes, kernel_size=1)

    def forward(self, x):
        z = self.in_proj(x)
        for layer in self.layers:
            z = layer(z)
        return self.out(z)


class MSTCN2(nn.Module):
    def __init__(self, in_dim: int, classes: int, hidden: int, num_stages: int, num_layers: int, dropout: float):
        super().__init__()
        self.stage0 = PredictionGeneration(in_dim, classes, hidden, num_layers, dropout)
        self.refines = nn.ModuleList(Refinement(classes, hidden, num_layers, dropout) for _ in range(num_stages - 1))
        self.register_buffer("norm_mean", torch.zeros(1, 1, in_dim))
        self.register_buffer("norm_std", torch.ones(1, 1, in_dim))

    def forward(self, x):
        x = (x - self.norm_mean) / self.norm_std
        out = self.stage0(x.transpose(1, 2))
        for refine in self.refines:
            out = refine(F.softmax(out, dim=1))
        return out.transpose(1, 2)


def load_model(path: Path) -> MSTCN2:
    """读训练状态 checkpoint 的 ``model_state``，strict 加载后返回 eval 模式的模型。"""
    model = MSTCN2(**STRUCTURE)
    checkpoint = torch.load(Path(path), map_location="cpu", weights_only=True)
    model.load_state_dict(checkpoint["model_state"], strict=True)
    return model.eval()


@torch.no_grad()
def predict_probs(model: MSTCN2, feats: np.ndarray) -> np.ndarray:
    """[T, 6] softmax 概率（CPU）。"""
    x = torch.from_numpy(np.asarray(feats, dtype=np.float32)).unsqueeze(0)
    return torch.softmax(model(x), dim=-1)[0].numpy().astype(np.float32)
