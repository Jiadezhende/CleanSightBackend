"""因果滑窗 GRU（推理部分），原样迁自旧 impl/clean.py。结构见 contract.STRUCTURE / WINDOW。"""

from __future__ import annotations

import numpy as np

WINDOW_BATCH = 1024


def build_model(input_dim: int, class_count: int, hidden: int = 128, num_layers: int = 3):
    """单向 3 层 GRU + 线性头，输入 `[B, window, F]`，取窗口末帧输出 `[B, C]`（state_dict 键 rnn.* / head.*）。"""
    import torch.nn as nn

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.rnn = nn.GRU(input_dim, hidden, num_layers=num_layers, batch_first=True, dropout=0.2)
            self.head = nn.Linear(hidden, class_count)

        def forward(self, x):
            out, _ = self.rnn(x)
            return self.head(out[:, -1])

    return Model()


def causal_windows(x: np.ndarray, window: int) -> np.ndarray:
    """`[T,F]` → 每帧一个以它为末帧的窗口 `[T,window,F]`；开头不足一窗的用首帧重复补齐。"""
    padded = np.concatenate([np.repeat(x[:1], window - 1, axis=0), x], axis=0)
    return np.lib.stride_tricks.sliding_window_view(padded, window, axis=0).transpose(0, 2, 1)
