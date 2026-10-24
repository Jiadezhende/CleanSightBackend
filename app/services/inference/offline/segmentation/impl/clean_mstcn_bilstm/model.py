"""MS-TCN + BiLSTM 网络（推理部分），原样迁自旧 impl/clean.py。结构超参写死，strict 加载。"""

from __future__ import annotations


def build_model(in_dim: int, class_count: int, hidden: int = 64):
    """构建 MS-TCN + BiLSTM 网络（BiLSTM 编码 → 单阶段 TCN → 两级 refine）。"""
    import torch
    import torch.nn as nn

    class DilatedResidualLayer(nn.Module):
        def __init__(self, channels: int, dilation: int, dropout: float):
            super().__init__()
            self.conv_dilated = nn.Conv1d(
                channels,
                channels,
                kernel_size=3,
                padding=dilation,
                dilation=dilation,
            )
            self.conv_1x1 = nn.Conv1d(channels, channels, kernel_size=1)
            self.norm = nn.BatchNorm1d(channels)
            self.dropout = nn.Dropout(dropout)
            self.act = nn.ReLU()

        def forward(self, x):
            out = self.conv_dilated(x)
            out = self.act(self.norm(out))
            out = self.conv_1x1(out)
            out = self.dropout(out)
            return self.act(x + out)

    class SingleStageTCN(nn.Module):
        def __init__(self, in_channels: int, classes: int, hidden: int, layers: int, dropout: float):
            super().__init__()
            self.input_projection = nn.Conv1d(in_channels, hidden, kernel_size=1)
            self.layers = nn.ModuleList(
                DilatedResidualLayer(hidden, dilation=2 ** i, dropout=dropout)
                for i in range(layers)
            )
            self.classifier = nn.Conv1d(hidden, classes, kernel_size=1)

        def forward(self, x):
            z = self.input_projection(x)
            for layer in self.layers:
                z = layer(z)
            return self.classifier(z)

    class Model(nn.Module):
        def __init__(self):
            super().__init__()
            self.input_norm = nn.LayerNorm(in_dim)
            self.input_projection = nn.Linear(in_dim, hidden)
            self.bilstm = nn.LSTM(
                input_size=hidden,
                hidden_size=hidden,
                num_layers=2,
                batch_first=True,
                bidirectional=True,
                dropout=0.15,
            )
            self.lstm_projection = nn.Conv1d(hidden * 2, hidden, kernel_size=1)
            self.first_stage = SingleStageTCN(hidden, class_count, hidden, 6, 0.15)
            self.refine_stages = nn.ModuleList(
                SingleStageTCN(class_count, class_count, hidden, 6, 0.15)
                for _ in range(2)
            )

        def forward(self, x):
            z = torch.relu(self.input_projection(self.input_norm(x)))
            z, _ = self.bilstm(z)
            z = self.lstm_projection(z.transpose(1, 2))
            logits = self.first_stage(z)
            for stage in self.refine_stages:
                logits = stage(torch.softmax(logits, dim=1))
            return logits

    return Model()
