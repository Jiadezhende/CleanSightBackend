"""离线分割策略契约：准备帧 → 构建模型输入 → 前向 → 后处理。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Generic, Iterator, Mapping, Sequence, TypeVar, final

from app.types.detection import FrameDetection
from app.types.frame import Frame
from app.types.temporal import LabelProbs, TemporalSegment

from .utils import FrameInputContract, prepare_frames, validate_label_probs_shape, validate_segments

ModelInputT = TypeVar("ModelInputT")

# 该 run 的 raw 轨取帧：`video(width=, height=, start_ts=None, end_ts=None) -> Iterator[Frame]`。
# 帧 ts 位级等于 detections 的 ts，可直接 `==` 匹配（`app.storage.hls.iter_frames`）。
VideoSource = Callable[..., Iterator[Frame]]


@dataclass(frozen=True)
class OfflineContext:
    """Runner 注入的 run 级资源；策略经它取数据。

    缓存是该 run 的中间产物（`{name: ndarray}`，随 run 目录 TTL 回收）：没有或读坏了 `load_cache` 返回 None；
    `save_cache` 写失败只告警。内容是否仍有效由策略自己核对。
    """

    video: VideoSource
    load_cache: Callable[[str], dict[str, Any] | None]
    save_cache: Callable[[str, Mapping[str, Any]], None]


class OfflineSegmenter(ABC, Generic[ModelInputT]):
    """离线全序列分割策略基类。构造参数全部来自 YAML `offline.params`。"""

    frame_contract: FrameInputContract
    max_duration_s: float | None = None

    @property
    def name(self) -> str:
        """策略身份 = 类名，即产出 `TemporalSegment.producer`。"""
        return type(self).__name__

    @final
    def infer(
        self, frames: Sequence[FrameDetection], context: OfflineContext,
    ) -> tuple[Sequence[TemporalSegment], LabelProbs | None]:
        """从按 ts 升序排列的只读帧推理，返回动作段和原始逐帧概率。"""
        if not frames:
            return [], None
        self._validate_required_materials()
        frames = prepare_frames(
            frames, self.frame_contract, max_duration_s=self.max_duration_s, producer=self.name,
        )
        model_input = self._build_model_input(frames, context)
        label_probs = self._predict(model_input)
        validate_label_probs_shape(label_probs)
        segments = self._postprocess(label_probs)
        validate_segments(segments, producer=self.name)
        return segments, label_probs

    def _validate_required_materials(self) -> None:
        """按需在帧准备前检查本次推理必需的物料。"""

    @abstractmethod
    def _build_model_input(self, frames: Sequence[FrameDetection], context: OfflineContext) -> ModelInputT:
        """把符合帧契约的检测序列转换为模型输入。"""
        raise NotImplementedError

    @abstractmethod
    def _predict(self, model_input: ModelInputT) -> LabelProbs:
        """运行模型，返回原始逐帧类别概率。"""
        raise NotImplementedError

    @abstractmethod
    def _postprocess(self, label_probs: LabelProbs) -> Sequence[TemporalSegment]:
        """从只读逐帧概率解码动作段。"""
        raise NotImplementedError

    def materials(self) -> Sequence[Path]:
        """本实例按构造参数解析出的必需物料文件（部署核对 / 启动预检用）。

        只拼路径：不读盘、不 import torch。不需要物料的策略保持默认空。
        """
        return ()
