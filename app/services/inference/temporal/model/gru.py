import torch.nn as nn

class GRUClassifier(nn.Module):
    def __init__(self, input_dim, num_classes, hidden=128, num_layers=3):
        super().__init__()
        self.rnn = nn.GRU(
            input_size=input_dim,
            hidden_size=hidden,
            num_layers=num_layers,
            batch_first=True,
            bidirectional=False,
        )
        self.head = nn.Linear(hidden, num_classes)

    def forward(self, x):
        # x: (B, T, F)
        out, _ = self.rnn(x)        # (B, T, H)
        return self.head(out)       # (B, T, num_classes)