"""离线时序模型通用接口。

本文件只定义离线链路的数据形状，不绑定具体模型实现。

输入：
    FeatureStore 中回读到的完整检测序列，整理为 OfflineFeatureSequence。
    每个 OfflineFrame 表示同一时间戳下一个或多个检测 source 的检测框。

输出：
    OfflineInferenceResult。它包含逐帧预测 frame_predictions，以及已经合并好的
    行为时间线 timeline。timeline 后续可转换为后端 SegmentFact 写入 FactLedger。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, Sequence

from app.domain.detection import Detection


@dataclass(frozen=True)
class OfflineFrame:
    """离线模型看到的一帧多源检测结果。

    timestamp:
        帧捕获时间戳，来自 features.jsonl 的 ts。
    detections_by_source:
        key 是检测 source 名，如 clean_large/clean_small；value 是该 source 的 Detection 列表。
    """

    timestamp: float
    detections_by_source: dict[str, list[Detection]]

    @property
    def detections(self) -> list[Detection]:
        """把多 source 检测摊平成一个列表，便于简单模型遍历。"""
        merged: list[Detection] = []
        for dets in self.detections_by_source.values():
            merged.extend(dets)
        return merged


@dataclass(frozen=True)
class OfflineFeatureSequence:
    """一次离线推理任务的完整输入序列。"""

    task_id: int
    step_id: int
    frames: list[OfflineFrame]
    sources: list[str]
    fps: float = 7.5
    meta: dict[str, object] = field(default_factory=dict)


@dataclass(frozen=True)
class FramePrediction:
    """模型对单帧的动作判断。"""

    timestamp: float
    label: str
    confidence: float
    scores: dict[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class TimelineSegment:
    """合并连续帧后得到的行为时间段。"""

    label: str
    start: float
    end: float
    confidence: float
    start_frame: int
    end_frame: int
    source: str
    meta: dict[str, object] = field(default_factory=dict)

    def to_json(self) -> dict[str, object]:
        """转换为可写 JSON 的结构化结果。"""
        return {
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "confidence": self.confidence,
            "start_frame": self.start_frame,
            "end_frame": self.end_frame,
            "source": self.source,
            "meta": self.meta,
        }


@dataclass(frozen=True)
class OfflineInferenceResult:
    """离线时序模型的完整输出。"""

    task_id: int
    step_id: int
    model_name: str
    model_version: str
    sources: list[str]
    frame_count: int
    frame_predictions: list[FramePrediction]
    timeline: list[TimelineSegment]

    def to_json(self) -> dict[str, object]:
        """转换为结构化推理结果，供 CLI 打印或落盘。"""
        return {
            "task_id": self.task_id,
            "step_id": self.step_id,
            "model_name": self.model_name,
            "model_version": self.model_version,
            "sources": self.sources,
            "frame_count": self.frame_count,
            "frame_predictions": [
                {
                    "timestamp": p.timestamp,
                    "label": p.label,
                    "confidence": p.confidence,
                    "scores": p.scores,
                }
                for p in self.frame_predictions
            ],
            "timeline": [segment.to_json() for segment in self.timeline],
        }


class OfflineTemporalModel(Protocol):
    """离线时序模型统一接口。

    后续 MS-TCN、ASFormer、BiGRU 或其它模型只要实现该接口，就能被 OfflineInferenceWorker
    统一调度。接口刻意保持简单：输入完整序列，输出逐帧预测；切段逻辑由 worker 统一处理。
    """

    name: str
    version: str
    labels: Sequence[str]

    def predict(self, sequence: OfflineFeatureSequence) -> list[FramePrediction]:
        """对完整时序序列做离线推理，返回逐帧预测。"""
