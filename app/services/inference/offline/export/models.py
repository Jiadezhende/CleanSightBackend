"""导出器的货币壳 —— 框架与 recipe 之间的唯一接缝。

三个壳，各管一段：
    ExportSpec    一次导出的输入（存储键 + recipe + backbone + 设备）
    VisualFrames  帧源产出、recipe 消费的**视觉**货币（bbox 侧走 FrameFeature，不重复造壳）
    ExportResult  一次导出的结果（含质量统计，直接进 manifest）

**recipe 统一签名**：`(frames: Sequence[FrameFeature], visual: Optional[VisualFrames]) -> ModelInput`

这条签名是本设计的关键接缝：`export/` 框架完全不认识某业务的特征列名，业务 recipe（住在
`offline/impl/<业务>.py`）完全不认识 HLS/sidecar/backbone。因此同一份 recipe 既能被导出器
调用产训练样例，也能被将来的 `Segmenter.preprocess` 调用做线上特征转换——单一真源是结构性
保证，不是纪律要求。

本模块刻意只依赖 numpy + dataclass：import 它不该拖起 torch。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np


@dataclass(frozen=True)
class VisualFrames:
    """一段视频的逐帧视觉特征；与 FrameFeature 序列**逐行同序等长**。

    深浅两层是同一次 backbone 前向的两个输出（见需求文档 §4.2.1）：
        deep    (stride-32) 全局池化 → 场景上下文，语义强
        shallow (stride-8)  RoIAlign → 手部细节（实测手框占 10.9×12.2 格，深层只有 2.7×3.1）

    Attributes:
        ts: 每行对应的帧 ts，与 FrameFeature.ts 同源同值
        deep: [T, C, H/32, W/32]；不消费深层特征的 recipe 为 None
        shallow: [T, C, H/8, W/8]；不消费浅层特征的 recipe 为 None
        valid: [T] bool。**False = 该帧取不到像素**（无 sidecar / 段不在 playlist / 帧已被淘汰）。
            取不到一律显式 mask 并计入统计，绝不用零向量、邻帧或插值冒充真实帧（不变式 F4）
        backbone: backbone 身份，写进 manifest 供追溯
    """

    ts: List[float]
    deep: Optional[np.ndarray] = None
    shallow: Optional[np.ndarray] = None
    valid: Optional[np.ndarray] = None
    backbone: str = "none"

    @property
    def frame_count(self) -> int:
        return len(self.ts)


@dataclass(frozen=True)
class ExportQuality:
    """一次导出的取帧质量统计。**必需项不是可选项**——一条 step 有多少帧其实没拿到像素，
    必须一眼看到，不能等到模型不收敛时才回头查。"""

    frames_total: int = 0
    needs_pixels: bool = False
    pixel_hit: int = 0          # 精确取到像素的帧数
    pixel_miss: int = 0         # 取不到像素、已 mask 的帧数（下三项之和 + 其他）
    no_sidecar: int = 0         # 所属段没有逐帧索引（旧数据 / 黑屏段 / 写失败）
    not_in_playlist: int = 0    # 所属段转码失败未进 playlist，帧不可取
    no_segment: int = 0         # 该 ts 落在任何 raw 段之外

    def to_json(self) -> Dict[str, object]:
        return {
            "frames_total": self.frames_total,
            "needs_pixels": self.needs_pixels,
            "pixel_hit": self.pixel_hit,
            "pixel_miss": self.pixel_miss,
            "no_sidecar": self.no_sidecar,
            "not_in_playlist": self.not_in_playlist,
            "no_segment": self.no_segment,
        }


@dataclass(frozen=True)
class ExportSpec:
    """一次导出的输入。

    Attributes:
        task_id / step_id: 稳定存储键，与离线分割链路同款（不接 client / CQ / DB）
        recipe: recipe 函数的**全限定路径**（如 `app...offline.impl.clean.export_r0`）。
            与 StageFactory 的 `offline.class` 及 CLI `--strategy` 同款约定——框架靠
            importlib 取，因而零业务知识
        backbone: backbone 身份；不消费像素的 recipe 传 None
        device: "cpu"（默认，不抢在线资源）/ "cuda"
        out_dir: 产物目录；None = 由 storage_base_dir 派生（见 runner）
    """

    task_id: int
    step_id: int
    recipe: str
    backbone: Optional[str] = None
    device: str = "cpu"
    out_dir: Optional[Path] = None


@dataclass(frozen=True)
class ExportResult:
    """一次导出的结果。status ∈ {completed, skipped}；异常经 run() 抛出，不落此结构
    （与 OfflineRunResult 同款约定）。"""

    status: str
    recipe: str
    frame_count: int = 0
    feature_dim: int = 0
    out_dir: Optional[Path] = None
    quality: ExportQuality = field(default_factory=ExportQuality)
    message: str = ""
