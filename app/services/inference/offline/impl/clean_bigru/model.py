"""BiGRU 网络（推理部分），原样迁自旧 impl/clean.py。结构超参写死，strict 加载。"""

from __future__ import annotations


def build_model(in_dim: int, class_count: int, hidden: int = 64):
    """构建 BiGRU 网络（3 层双向 GRU → 时序卷积头）。"""
    import torch
    import torch.nn as nn

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_norm = nn.LayerNorm(in_dim)
            self.projection = nn.Linear(in_dim, hidden)
            self.gru = nn.GRU(hidden, hidden, num_layers=3, batch_first=True, bidirectional=True, dropout=0.15)
            self.temporal_head = nn.Sequential(
                nn.Conv1d(hidden * 2, hidden, kernel_size=3, padding=1),
                nn.ReLU(),
                nn.Dropout(0.15),
                nn.Conv1d(hidden, class_count, kernel_size=1),
            )

        def forward(self, x):
            z = torch.relu(self.projection(self.input_norm(x)))
            z, _ = self.gru(z)
            return self.temporal_head(z.transpose(1, 2))

    return Model()
