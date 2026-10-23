"""ASFormer 风格 网络（推理部分），原样迁自旧 impl/clean.py。结构超参写死，strict 加载。"""

from __future__ import annotations


def build_model(in_dim: int, class_count: int, hidden: int = 64, heads: int = 4):
    """构建 ASFormer 风格网络（局部卷积 + 多头自注意力 + FFN，带正弦位置编码）。"""
    import math
    import torch
    import torch.nn as nn

    def sinusoidal_position(length: int, dim: int, device):
        pos = torch.arange(length, device=device).float().unsqueeze(1)
        idx = torch.arange(dim, device=device).float().unsqueeze(0)
        div = torch.exp(torch.floor(idx / 2) * (-math.log(10000.0) / max(dim, 1)))
        enc = pos * div
        out = torch.zeros(length, dim, device=device)
        out[:, 0::2] = torch.sin(enc[:, 0::2])
        out[:, 1::2] = torch.cos(enc[:, 1::2])
        return out

    class Block(nn.Module):
        def __init__(self, dilation: int):
            super().__init__()
            self.local = nn.Conv1d(hidden, hidden, kernel_size=3, padding=dilation, dilation=dilation)
            self.local_norm = nn.LayerNorm(hidden)
            self.attn = nn.MultiheadAttention(hidden, heads, dropout=0.15, batch_first=True)
            self.attn_norm = nn.LayerNorm(hidden)
            self.ffn = nn.Sequential(
                nn.Linear(hidden, hidden * 4),
                nn.GELU(),
                nn.Dropout(0.15),
                nn.Linear(hidden * 4, hidden),
            )
            self.ffn_norm = nn.LayerNorm(hidden)
            self.dropout = nn.Dropout(0.15)

        def forward(self, x):
            local = self.local(x.transpose(1, 2)).transpose(1, 2)
            x = self.local_norm(x + self.dropout(torch.relu(local)))
            attn, _ = self.attn(x, x, x, need_weights=False)
            x = self.attn_norm(x + self.dropout(attn))
            return self.ffn_norm(x + self.dropout(self.ffn(x)))

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_norm = nn.LayerNorm(in_dim)
            self.projection = nn.Linear(in_dim, hidden)
            self.blocks = nn.ModuleList([Block(2 ** (i % 4)) for i in range(4)])
            self.classifier = nn.Sequential(
                nn.LayerNorm(hidden),
                nn.Linear(hidden, class_count),
            )

        def forward(self, x):
            _, time, _ = x.shape
            z = self.projection(self.input_norm(x))
            z = z + sinusoidal_position(time, z.shape[-1], x.device).unsqueeze(0)
            for block in self.blocks:
                z = block(z)
            return self.classifier(z).transpose(1, 2)

    return Model()
