"""离线分割策略基类 —— 两段接缝：输入预处理层（预留）+ 模型推理分割。

一个离线推理策略 = 一个 `OfflineSegmenter` 子类（对齐在线 Detector/Operator「加一子类」范式，
见 docs/kb/DESIGN_EXTENDING_DETECTION.md）。策略实现全部收在 `offline/segmenters/`，框架层
（segmenter/runner/cli）不掺实现。

管线两段：
    load(task_id, step_id) → List[FrameDetection]（帧级、多流已对齐、按 ts 升序）
        │
        ▼ preprocess(frames)    ← 输入预处理层（预留）：raw bbox 序列不一定能直接喂模型，
        │                          需张量化/归一化/时间降采样/定长编码的模型在此转换
        ▼ segment(model_input)  ← 模型推理 + 解码为 TemporalSegment
        │
        ▼ List[TemporalSegment]

约束：
- `frames` 只读，不得原地修改；
- 策略不访问存储 / ClientManager / CQ / 数据库（纯算法）；
- 输出每条 `TemporalSegment.producer` 必须等于本策略 `name`（= 类名）；`start <= end`、时间为有限数、
  `0 <= conf <= 1`（由 Runner 统一校验，见 runner.py）。
- 输入吃 `FrameDetection`、输出吐 `TemporalSegment`（两者都在 `app.domain`，与在线同型），
  不自定义中间数据壳。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, List, Optional, Sequence

from app.domain.detection import FrameDetection
from app.domain.temporal import LabelProbs, TemporalSegment


class OfflineSegmenter(ABC):
    """离线全序列分割策略基类。构造参数全部来自 YAML `offline.params`。"""

    @property
    def name(self) -> str:
        """策略身份 = 类名，即产出 `TemporalSegment.producer`。"""
        return type(self).__name__

    @abstractmethod
    def preprocess(self, frames: Sequence[FrameDetection]) -> Any:
        """输入预处理接口：把帧级 FrameDetection 序列转成模型可消费的输入。

        基类只约束调用形状，不做默认特征工程。bbox 归一化、top-k 目标选择、
        speed 的 dt 计算、tensor 化、权重加载等都应由具体策略在自己的单文件里完成。
        `frames` 已按 ts 升序、多流在各 `FrameDetection.by_source` 内对齐（load 保证）。
        """
        raise NotImplementedError

    @abstractmethod
    def segment(self, model_input: Any) -> List[TemporalSegment]:
        """消费 `preprocess` 的输出，做模型推理并解码为动作分段事实。"""
        raise NotImplementedError

    def label_probs(self) -> Optional[LabelProbs]:
        """可选旁路：上一次 `segment()` 的逐帧类别概率，仅供可视化，默认无。

        产逐帧概率的模型 override；Runner 拿到非 None 时先于 temporal.jsonl 落 `label_probs.npz`。
        它不参与任何判断，也不是契约的一部分——规则型策略保持默认 None 即可。
        """
        return None
