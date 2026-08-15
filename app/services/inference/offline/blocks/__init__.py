"""特征块工具层 —— **不是管线阶段，是一组工具**。

对外只有五个符号：

    BlockKind       取哪种块（枚举而非字符串：拼错在导入期就炸）
    load()          给 (kind, task, step[, backbone]) 返回一块 FeatureBlock
    sweep_cache()   每次离线执行开头调一次，清过期缓存
    NoFeatures      该 step 没有特征（调用方翻译成 skipped，不覆盖旧事实）
    load_frames()   原始检测帧的逃生口，只给规则型/调试型策略（见其 docstring）

Segmenter 按自己的 `needs` 直接调 `load()`，编排层（cli）不认识块也不认识 FeatureStore。
块的构建、加载、缓存、回收全部收在本包内，方向是 `infer → blocks` 单向。

代价分布决定了缓存策略：bbox 块是纯 numpy（1886 帧实测 16 ms），直算；视觉块要解码 +
backbone 前向（分钟级），进 `.cache`。缓存只给真正贵的那一路，不搞通用块存储。
"""

from __future__ import annotations

import logging
from enum import Enum
from pathlib import Path
from typing import Optional, Sequence, Tuple

from app.services.inference.offline.blocks import bbox, cache, visual
from app.services.inference.offline.blocks.frame_source import FetchStats
from app.services.inference.offline.models import FeatureBlock

logger = logging.getLogger(__name__)


class BlockKind(str, Enum):
    """块的种类。`VHAND`（R2 手部 token）尚未实现，占位是为了让 `needs` 声明有处可写。"""

    BBOX = "bbox"
    VGLOBAL = "vglobal"
    VHAND = "vhand"


class NoFeatures(Exception):
    """该 (task, step) 没有可用特征。由 cli 翻译成 skipped —— **不覆盖已有事实**。

    与「跑出来是空结果」严格区分：后者是 completed，会清掉该 producer 的旧分段。
    """


def load(
    kind: BlockKind,
    task_id: int,
    step_id: int,
    *,
    sources: Optional[Sequence[str]] = None,
    backbone: Optional[str] = None,
    device: str = "cpu",
    storage_dir: Optional[Path] = None,
    offline_dir: Optional[Path] = None,
    stats: Optional[FetchStats] = None,
    use_cache: bool = True,
) -> FeatureBlock:
    """取一块特征。

    Args:
        sources: 订阅的 detector name 列表；任一没有数据即抛 `NoFeatures`（保住
            「无输入 → skipped，不覆盖旧事实」的语义）
        backbone: 视觉块必填；bbox 块忽略
        stats: 取帧质量统计出参，只有视觉块会填

    Raises:
        NoFeatures: 该 step 无特征，或订阅的某个 source 无数据
        ValueError: 视觉块未指定 backbone；或块类型尚未实现
    """
    frames = load_frames(task_id, step_id, sources, storage_dir)
    size = _frame_size(frames)
    if kind is BlockKind.BBOX:
        # bbox 只做空间归一化，缺分辨率时用兜底值是可接受的近似（逐帧还会各自回退）
        width, height = size or (bbox.DEFAULT_FRAME_WIDTH, bbox.DEFAULT_FRAME_HEIGHT)
        return bbox.build(frames, width, height)
    if kind is BlockKind.VGLOBAL:
        if not backbone:
            raise ValueError("视觉块必须指定 backbone")
        if size is None:
            # 视觉块不能靠猜：rawvideo 管道按尺寸切帧，猜错就是整段像素错位
            raise ValueError("features 未记录帧分辨率，无法解码取帧")
        width, height = size
        return visual.build(
            task_id, step_id, [ff.ts for ff in frames], width, height,
            backbone, device, storage_dir, offline_dir, stats, use_cache,
        )
    raise ValueError(f"块类型 {kind} 尚未实现")


def sweep_cache(ttl_days: int = cache.DEFAULT_TTL_DAYS, **kwargs) -> int:
    """清理过期缓存与残留临时文件，返回释放字节数。"""
    return cache.sweep(ttl_days, **kwargs)


def load_frames(
    task_id: int, step_id: int, sources: Optional[Sequence[str]], storage_dir: Optional[Path]
):
    """从 FeatureStore 读整条序列并做订阅检查。

    FeatureStore 只在本包内被触碰——这是「编排层不认识块」得以成立的另一半。

    **这是给规则型/调试型策略的逃生口**（它们要看原始检测框而非 71 维特征）。模型型
    Segmenter 一律不得走这条路：特征只能来自 `load()`，否则单一真源（不变式 N1）失守。
    """
    from app.services.inference.feature.store import FeatureStore
    from app.settings import settings

    base = Path(storage_dir) if storage_dir is not None else settings.storage_base_dir
    frames = FeatureStore(base).load(task_id, step_id)
    if not frames:
        raise NoFeatures(f"task={task_id} step={step_id} 无特征（features.jsonl 缺失或为空）")
    if sources:
        present = set().union(*(ff.by_source.keys() for ff in frames))
        missing = [s for s in sources if s not in present]
        if missing:
            raise NoFeatures(f"订阅 source 无特征: {missing}")
    return frames


def _frame_size(frames) -> Optional[Tuple[int, int]]:
    """取源帧分辨率。逐帧常量，取首个非空；一个都没记录返回 None（由调用方决定能否兜底）。"""
    for ff in frames:
        if ff.frame_width and ff.frame_height:
            return int(ff.frame_width), int(ff.frame_height)
    return None


__all__ = ["BlockKind", "NoFeatures", "FeatureBlock", "FetchStats", "load", "sweep_cache", "load_frames"]
