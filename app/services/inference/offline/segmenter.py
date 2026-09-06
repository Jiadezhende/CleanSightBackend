"""离线分割策略基类 —— 两段接缝：输入预处理层（预留）+ 模型推理分割。

一个离线推理策略 = 一个 `OfflineSegmenter` 子类（对齐在线 Detector/Operator「加一子类」范式，
见 docs/kb/DESIGN_EXTENDING_DETECTION.md）。策略实现全部收在 `offline/segmenters/`，框架层
（segmenter/runner/cli）不掺实现。

管线两段：
    load(task_id, step_id) → List[FrameFeature]（帧级、多流已对齐、按 ts 升序）
        │
        ▼ estimate_memory_mb(frames)  ← 可选：报成本，由 Runner 对内存预算判准入（超则 skipped）
        │
        ▼ preprocess(frames)    ← 输入预处理层（预留）：raw bbox 序列不一定能直接喂模型，
        │                          需张量化/归一化/时间降采样/定长编码的模型在此转换
        ▼ segment(model_input)  ← 模型推理 + 解码为 SegmentFact
        │
        ▼ List[SegmentFact]

约束：
- `frames` 只读，不得原地修改；
- 策略不访问 FeatureStore / FactLedger / ClientManager / CQ / 数据库（纯算法）；
- 输出每条 `SegmentFact.source` 必须等于本策略 `name`；`start <= end`、时间为有限数、`0 <= conf <= 1`
  （由 Runner 统一校验，见 runner.py）。
- 输入吃 `FrameFeature`（domain，与在线滑窗同型）、输出吐 `SegmentFact`（inference.types），
  不自定义中间数据壳。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Optional, Sequence

from app.domain.detection import FrameFeature
from app.services.inference.types import SegmentFact


class OfflineSegmenter(ABC):
    """离线全序列分割策略基类。"""

    def __init__(self, name: str, subscribes: Sequence[str]):
        if not name:
            raise ValueError("offline segmenter name is required")
        if not subscribes:
            raise ValueError("offline segmenter subscribes is required")
        self.name = name
        self.subscribes: List[str] = list(subscribes)

    @abstractmethod
    def preprocess(self, frames: Sequence[FrameFeature]) -> Any:
        """输入预处理接口：把帧级 FrameFeature 序列转成模型可消费的输入。

        基类只约束调用形状，不做默认特征工程。bbox 归一化、top-k 目标选择、
        speed 的 dt 计算、tensor 化、权重加载等都应由具体策略在自己的单文件里完成。
        `frames` 已按 ts 升序、多流在各 `FrameFeature.by_source` 内对齐（load 保证）。
        """
        raise NotImplementedError

    @abstractmethod
    def segment(self, model_input: Any) -> List[SegmentFact]:
        """消费 `preprocess` 的输出，做模型推理并解码为动作分段事实。"""
        raise NotImplementedError

    def estimate_memory_mb(self, frames: Sequence[FrameFeature]) -> Optional[float]:
        """可选：预估「本策略跑完这段输入」的峰值常驻内存（MB）。默认 None = 不报成本。

        **策略层报成本、框架层判**：Runner 在 preprocess 之前调用本方法，超出内存预算时
        直接返回 `skipped`（不抛异常、不覆盖旧事实），见 runner.py。返回 None 的策略不被拦
        —— 轻量/规则型策略无需为此写一份估算。

        估算须只依赖 load 后已知的量（帧数、检测框总数），不得跑真实 preprocess。
        它是模型不是测量，会随特征管线与模型结构漂移，实现方须用单测把它钉在实测点上。
        """
        return None

    def debug_result(self) -> Optional[dict]:
        """可选：返回上一次 `segment()` 的逐帧调试产物（纯 dict），默认无。

        产逐帧预测的策略可 override，返回 `{"frame_predictions": [...], "segments": [...]}`；
        Runner 若拿到非 None，会补 task/step 落 `offline_inference_result.json`（见 runner.py）。
        presence 型等无逐帧语义的策略保持默认 None → 不落该文件。
        """
        return None
