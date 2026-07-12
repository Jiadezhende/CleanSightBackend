import torch
import torch.nn as nn

class TransformerClassifier(nn.Module):
    def __init__(self, input_dim, num_classes, d_model=128, nhead=4, num_layers=3):
        super().__init__()
        self.proj = nn.Linear(input_dim, d_model)
        encoder_layer = nn.TransformerEncoderLayer(
            d_model=d_model, nhead=nhead, dim_feedforward=256,
            batch_first=True
        )
        self.transformer = nn.TransformerEncoder(encoder_layer, num_layers=num_layers)
        self.head = nn.Linear(d_model, num_classes)

    def forward(self, x, padding_mask=None):
        # x: (B, T, F)
        B, T, _ = x.shape
        x = self.proj(x)

        causal_mask = torch.triu(
            torch.ones(T, T, device=x.device) * float('-inf'),
            diagonal=1
        )
        out = self.transformer(x, mask=causal_mask, src_key_padding_mask=padding_mask)

        return self.head(out)