"""MS-TCN++ runtime wrapper for online phase recognition.

This module keeps the production inference path independent from the research
folder name ``MS-TCN2``.  It mirrors the trained network architecture and
exposes a small ``predict(features)`` API used by TemporalAnalyzer classes.
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path
from typing import Any, Dict, List

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

logger = logging.getLogger(__name__)


class MSTCN2Model(nn.Module):
    """MS-TCN++ model used by the offline training code."""

    def __init__(
        self,
        num_layers_pg: int,
        num_layers_r: int,
        num_r: int,
        num_f_maps: int,
        dim: int,
        num_classes: int,
    ):
        super().__init__()
        self.pg = PredictionGeneration(num_layers_pg, num_f_maps, dim, num_classes)
        self.rs = nn.ModuleList([
            copy.deepcopy(Refinement(num_layers_r, num_f_maps, num_classes, num_classes))
            for _ in range(num_r)
        ])

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.pg(x)
        outputs = out.unsqueeze(0)
        for refinement in self.rs:
            out = refinement(F.softmax(out, dim=1))
            outputs = torch.cat((outputs, out.unsqueeze(0)), dim=0)
        return outputs


class PredictionGeneration(nn.Module):
    """Initial temporal prediction stage."""

    def __init__(self, num_layers: int, num_f_maps: int, dim: int, num_classes: int):
        super().__init__()
        self.num_layers = num_layers
        self.conv_1x1_in = nn.Conv1d(dim, num_f_maps, 1)
        self.conv_dilated_1 = nn.ModuleList([
            nn.Conv1d(
                num_f_maps,
                num_f_maps,
                3,
                padding=2 ** (num_layers - 1 - i),
                dilation=2 ** (num_layers - 1 - i),
            )
            for i in range(num_layers)
        ])
        self.conv_dilated_2 = nn.ModuleList([
            nn.Conv1d(num_f_maps, num_f_maps, 3, padding=2 ** i, dilation=2 ** i)
            for i in range(num_layers)
        ])
        self.conv_fusion = nn.ModuleList([
            nn.Conv1d(2 * num_f_maps, num_f_maps, 1) for _ in range(num_layers)
        ])
        self.dropout = nn.Dropout()
        self.conv_out = nn.Conv1d(num_f_maps, num_classes, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        features = self.conv_1x1_in(x)
        for i in range(self.num_layers):
            residual = features
            features = self.conv_fusion[i](
                torch.cat(
                    [self.conv_dilated_1[i](features), self.conv_dilated_2[i](features)],
                    dim=1,
                )
            )
            features = F.relu(features)
            features = self.dropout(features)
            features = features + residual
        return self.conv_out(features)


class Refinement(nn.Module):
    """Refinement stage that smooths and corrects temporal predictions."""

    def __init__(self, num_layers: int, num_f_maps: int, dim: int, num_classes: int):
        super().__init__()
        self.conv_1x1 = nn.Conv1d(dim, num_f_maps, 1)
        self.layers = nn.ModuleList([
            copy.deepcopy(DilatedResidualLayer(2 ** i, num_f_maps, num_f_maps))
            for i in range(num_layers)
        ])
        self.conv_out = nn.Conv1d(num_f_maps, num_classes, 1)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.conv_1x1(x)
        for layer in self.layers:
            out = layer(out)
        return self.conv_out(out)


class DilatedResidualLayer(nn.Module):
    """Dilated temporal convolution with residual connection."""

    def __init__(self, dilation: int, in_channels: int, out_channels: int):
        super().__init__()
        self.conv_dilated = nn.Conv1d(
            in_channels, out_channels, 3, padding=dilation, dilation=dilation
        )
        self.conv_1x1 = nn.Conv1d(out_channels, out_channels, 1)
        self.dropout = nn.Dropout()

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = F.relu(self.conv_dilated(x))
        out = self.conv_1x1(out)
        out = self.dropout(out)
        return x + out


class MSTCNRuntime:
    """Load a trained MS-TCN++ checkpoint and run phase prediction."""

    def __init__(
        self,
        model_path: str,
        mapping_path: str,
        feature_dim: int = 20,
        num_layers_pg: int = 10,
        num_layers_r: int = 10,
        num_r: int = 3,
        num_f_maps: int = 64,
        device: str = "auto",
    ):
        self.model_path = _resolve_project_path(model_path)
        self.mapping_path = _resolve_project_path(mapping_path)
        self.feature_dim = feature_dim
        self.id_to_label = self._load_mapping(self.mapping_path)
        self.device = self._resolve_device(device)

        if not self.model_path.exists():
            raise FileNotFoundError(f"MS-TCN model not found: {self.model_path}")

        self.model = MSTCN2Model(
            num_layers_pg=num_layers_pg,
            num_layers_r=num_layers_r,
            num_r=num_r,
            num_f_maps=num_f_maps,
            dim=feature_dim,
            num_classes=len(self.id_to_label),
        ).to(self.device)
        state_dict = torch.load(self.model_path, map_location=self.device)
        state_dict = _normalize_state_dict_keys(state_dict)
        self.model.load_state_dict(state_dict)
        self.model.eval()
        logger.info(
            "[MSTCNRuntime] Loaded model=%s classes=%s device=%s",
            self.model_path,
            self.id_to_label,
            self.device,
        )

    def predict(self, features: np.ndarray) -> Dict[str, Any]:
        """Predict per-frame phase labels.

        Args:
            features: ``np.ndarray`` with shape ``[feature_dim, T]``. If the
                caller accidentally passes ``[T, feature_dim]``, it is
                transposed for convenience.
        """
        features = np.asarray(features, dtype=np.float32)
        if features.ndim != 2:
            raise ValueError(f"MS-TCN features must be 2-D, got shape={features.shape}")
        if features.shape[0] != self.feature_dim and features.shape[1] == self.feature_dim:
            features = features.T
        if features.shape[0] != self.feature_dim:
            raise ValueError(
                f"MS-TCN feature dim mismatch: expected {self.feature_dim}, got {features.shape}"
            )
        if features.shape[1] == 0:
            raise ValueError("MS-TCN feature sequence is empty")

        input_x = torch.from_numpy(features).unsqueeze(0).to(self.device)
        with torch.no_grad():
            logits = self.model(input_x)[-1]
            probs = F.softmax(logits, dim=1)
            confidences, predicted = torch.max(probs, dim=1)

        ids = predicted.squeeze(0).detach().cpu().numpy().astype(int).tolist()
        confs = confidences.squeeze(0).detach().cpu().numpy().astype(float).tolist()
        labels = [self.id_to_label[i] for i in ids]
        current_index = len(labels) - 1
        return {
            "ids": ids,
            "labels": labels,
            "confidences": confs,
            "current_id": ids[current_index],
            "current_label": labels[current_index],
            "confidence": float(confs[current_index]),
        }

    @staticmethod
    def _load_mapping(mapping_path: Path) -> List[str]:
        if not mapping_path.exists():
            raise FileNotFoundError(f"MS-TCN mapping not found: {mapping_path}")
        items: Dict[int, str] = {}
        for line in mapping_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            idx, label = line.split(maxsplit=1)
            items[int(idx)] = label
        if not items:
            raise ValueError(f"MS-TCN mapping is empty: {mapping_path}")
        return [items[i] for i in sorted(items)]

    @staticmethod
    def _resolve_device(device: str) -> torch.device:
        if device == "auto":
            return torch.device("cuda" if torch.cuda.is_available() else "cpu")
        return torch.device(device)


def _resolve_project_path(path: str) -> Path:
    p = Path(path)
    if p.is_absolute():
        return p
    project_root = Path(__file__).resolve().parents[4]
    return (project_root / p).resolve()


def _normalize_state_dict_keys(state_dict: Dict[str, Any]) -> Dict[str, Any]:
    """Map original research checkpoint keys to this module's attribute names."""
    normalized: Dict[str, Any] = {}
    for key, value in state_dict.items():
        if key.startswith("PG."):
            key = "pg." + key[3:]
        elif key.startswith("Rs."):
            key = "rs." + key[3:]
        normalized[key] = value
    return normalized
