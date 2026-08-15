"""视觉特征块：按 ts 反查 raw 像素帧 → backbone 前向 → 深层全局池化向量。

    ts 列表 ──FrameSource──> 逐段像素批 ──backbone.forward──> deep ──global_pool──> [T, C]

这是整条离线链路里**唯一贵的一段**（解码 + 前向，分钟级），因此也是唯一值得缓存的块：
同一条 step 换特征方案、换模型都不必重跑，只有换 backbone 才失效。缓存 key 就是
`(task, step, backbone)`，直接当文件名——不需要内容寻址，因为整棵缓存树都可重建。

取不到像素的帧：特征行留零，**语义由 `valid` 承载**——零值本身不表达「画面里什么都没有」
（不变式 F4）。绝不用邻帧或插值冒充真实帧。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional, Sequence

import numpy as np

from app.services.inference.offline.blocks import cache
from app.services.inference.offline.blocks.frame_source import FetchStats, FrameSource
from app.services.inference.offline.models import FeatureBlock

logger = logging.getLogger(__name__)

KIND = "vglobal"


def build(
    task_id: int,
    step_id: int,
    ts: Sequence[float],
    frame_width: int,
    frame_height: int,
    backbone: str,
    device: str = "cpu",
    storage_dir: Optional[Path] = None,
    offline_dir: Optional[Path] = None,
    stats: Optional[FetchStats] = None,
    use_cache: bool = True,
) -> FeatureBlock:
    """取像素 + 跑 backbone → 深层全局池化块 `[T, C]`。

    Args:
        stats: 取帧质量统计的出参（与 `FrameSource.iter_batches` 同款约定）。缓存命中时
            由 npz 头回填，故命中与否对调用方一致。
        use_cache: 关掉可强制重算（换 backbone 实现、验证缓存一致性时用）

    Raises:
        RuntimeError: 一帧像素都没取到——视觉分支整体不可用，硬失败而非产出全零块。
    """
    path = _cache_path(task_id, step_id, backbone, offline_dir)
    if use_cache:
        hit = cache.read_block(path)
        if hit is not None and hit[0].frame_count == len(ts):
            block, extra = hit
            _fill_stats(stats, extra)
            logger.info(
                "[blocks.visual] 缓存命中 task=%s step=%s backbone=%s → %s",
                task_id, step_id, backbone, path,
            )
            return block

    block, fetched = _forward(
        task_id, step_id, ts, frame_width, frame_height, backbone, device, storage_dir
    )
    _fill_stats(stats, vars(fetched))
    if use_cache:
        cache.write_block(path, block, extra=vars(fetched))
    return block


def _cache_path(
    task_id: int, step_id: int, backbone: str, offline_dir: Optional[Path]
) -> Path:
    return cache.cache_dir(task_id, step_id, offline_dir) / f"{KIND}_{sanitize(backbone)}.npz"


def sanitize(backbone: str) -> str:
    """backbone 标识可能带路径与后缀（`yolo:clean-large-best.pt`），净化成文件名安全形式。"""
    return (
        (backbone or "none").replace(".pt", "")
        .replace(":", "-").replace("/", "-").replace("\\", "-")
    )


def _fill_stats(stats: Optional[FetchStats], values) -> None:
    if stats is None:
        return
    for key, value in dict(values).items():
        if hasattr(stats, key):
            setattr(stats, key, int(value))


def _forward(
    task_id: int,
    step_id: int,
    ts: Sequence[float],
    frame_width: int,
    frame_height: int,
    backbone: str,
    device: str,
    storage_dir: Optional[Path],
) -> tuple[FeatureBlock, FetchStats]:
    """**按段流式处理**：解一段 → 前向一批 → 只留降维结果，全程不驻留整段像素与特征图。"""
    # 重依赖（torch / ultralytics / ffmpeg 子进程）延迟到真要视觉特征时才引入
    from app.services.inference.offline.blocks.backbone import global_pool, load_backbone
    from app.settings import settings

    base = Path(storage_dir) if storage_dir is not None else settings.storage_base_dir
    net = load_backbone(backbone, device)
    source = FrameSource(base)
    fetched = FetchStats()

    total = len(ts)
    valid = np.zeros(total, dtype=bool)
    values: Optional[np.ndarray] = None

    for out_idx, batch in source.iter_batches(
        task_id, step_id, list(ts), frame_width, frame_height, fetched
    ):
        deep, _shallow = net.forward(batch)  # R1 只需深层；浅层不物化（R2 才用）
        pooled = global_pool(deep)
        if values is None:  # 首批到手才知道通道数，避免把维度写死在框架里
            values = np.zeros((total, pooled.shape[1]), dtype=np.float32)
        values[out_idx] = pooled
        valid[out_idx] = True

    if values is None:  # 一帧都没取到：硬失败而非产出全零块
        raise RuntimeError(
            f"task={task_id} step={step_id} 没有取到任何像素帧"
            f"（no_sidecar={fetched.no_sidecar} not_in_playlist={fetched.not_in_playlist} "
            f"no_segment={fetched.no_segment}）；该 step 可能录制于 sidecar 落地之前"
        )
    logger.info(
        "[blocks.visual] 取帧 %d/%d 命中（no_sidecar=%d not_in_playlist=%d no_segment=%d decode_short=%d）",
        fetched.pixel_hit, total, fetched.no_sidecar, fetched.not_in_playlist,
        fetched.no_segment, fetched.decode_short,
    )
    block = FeatureBlock(
        values=values,
        names=[f"visual_global_{i}" for i in range(values.shape[1])],
        ts=[float(v) for v in ts],
        valid=valid,
        version=f"visual_global@{net.name}",
        spans={KIND: [0, int(values.shape[1])]},
    )
    return block, fetched
