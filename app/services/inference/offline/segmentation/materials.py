"""离线策略物料预检：每个 stage 当前激活的那个离线策略要哪些文件、在不在盘上。

    statuses = check_materials(config)                  # List[MaterialStatus]
    missing = [s for s in statuses if not s.exists]

只构造策略、调 `materials()` 拼路径，不加载权重、不 import torch。消费方：`cli materials`（部署核对）、
`OfflineJobService.start()`（缺失只告警）。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import List

from app.services.inference.config import InferenceConfig


@dataclass(frozen=True)
class MaterialStatus:
    stage: str
    producer: str
    path: Path
    exists: bool


def check_materials(config: InferenceConfig) -> List[MaterialStatus]:
    """逐 stage（配了 offline 的）列出激活策略的物料与是否存在；策略构造失败照常抛。"""
    from app.services.inference.stage_factory import StageFactory

    factory = StageFactory(config)
    out: List[MaterialStatus] = []
    for stage in config.list_stages():
        segmenter = factory.create_offline_segmenter(stage)
        if segmenter is None:
            continue
        for path in segmenter.materials():
            path = Path(path)
            out.append(MaterialStatus(stage, segmenter.name, path, path.is_file()))
    return out
