"""Operator — 实时流算子基类（合并 analyze + judge，共享一份状态机）。

流处理框架的处理单元（规则粒度，一个 Operator = 一条规则）：
- 订阅 N 条上游流（subscribes，流名 = detector.name），按感受野 window_seconds 取历史。
- analyze(windows)：消费订阅流、推进共享状态机 self._sm（不返回，状态写入 _sm）。
- judge()：读 self._sm 出规则结果（overlay 文案 + 实时告警）。
- finalize()：任务 terminate 时结算一次。

两接口共享同一 self._sm —— 不再有 EventFact 作为对象间传输、不再有双状态机同步。
每个 Client 独立实例，不跨 Client 共享（start_workflow() 时新建）。

身份维度：
- name      : 算子自身/输出身份（非输入）。当下仅日志/告警归属；将来算子→算子链时 = 其输出流名。
- subscribes: 输入流清单，显式必填（无 [name] 默认）。算子名 ≠ 流名。
- window_seconds: 感受野，算子需要多长历史；算子在 analyze 内 _clip 到此长度。
"""

from __future__ import annotations

import logging
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List, Tuple

from app.domain.alarm import Alarm
from app.domain.detection import FrameDetections

logger = logging.getLogger(__name__)


@dataclass
class AlignedFrame:
    """多流按 ts 对齐后的一帧：同一 ts 下各订阅流的 FrameDetections。"""

    ts: float
    by_source: Dict[str, FrameDetections]


class Operator(ABC):
    """实时流算子基类（合并 analyze + judge，单 _sm）。"""

    def __init__(self, name: str, subscribes: List[str], window_seconds: float):
        if not subscribes:
            raise ValueError(f"Operator '{name}' 必须显式声明 subscribes（输入流）")
        self.name = name
        self.subscribes = list(subscribes)
        self.window_seconds = float(window_seconds)
        self._sm: Dict[str, Any] = {}  # 子类 __init__ 负责完整初始化

    # ── 两接口（共享 self._sm）────────────────────────────────
    @abstractmethod
    def analyze(self, windows: Dict[str, List[FrameDetections]]) -> None:
        """消费订阅流、推进 self._sm。windows: {流名: 该流滑窗快照(按 ts 升序)}。"""

    @abstractmethod
    def judge(self) -> Tuple[List[str], List[Alarm]]:
        """读 self._sm 出 (overlay 文案, 实时告警)。"""

    def finalize(self) -> List[Alarm]:
        """结算：任务 terminate 时调用一次，产出结算告警。默认返空。"""
        return []

    # ── 基类工具 ─────────────────────────────────────────────
    def _clip(self, window: List[FrameDetections]) -> List[FrameDetections]:
        """把流裁到本算子感受野（保留 ts >= latest - window_seconds）。"""
        if not window:
            return []
        cutoff = window[-1].timestamp - self.window_seconds
        return [d for d in window if d.timestamp >= cutoff]

    def primary_window(
        self, windows: Dict[str, List[FrameDetections]]
    ) -> List[FrameDetections]:
        """单订阅便捷：取首个订阅流并裁到感受野。"""
        return self._clip(windows.get(self.subscribes[0], []))

    def _zip_by_ts(
        self, windows: Dict[str, List[FrameDetections]]
    ) -> List[AlignedFrame]:
        """多流按 ts 对齐（inner-join：仅保留所有订阅流都到齐的 ts）。

        依赖不变式：同帧多流的 ts 完全相等（来自同一 FrameInference.timestamp），
        故可用 ts 做精确 key。缺帧/错帧的 ts 被丢弃（latest-wins 等策略留子类 override）。
        """
        clipped = {s: self._clip(windows.get(s, [])) for s in self.subscribes}
        if any(not w for w in clipped.values()):
            return []
        indexed = {s: {d.timestamp: d for d in w} for s, w in clipped.items()}
        common = set.intersection(*(set(idx.keys()) for idx in indexed.values()))
        return [
            AlignedFrame(ts=ts, by_source={s: indexed[s][ts] for s in self.subscribes})
            for ts in sorted(common)
        ]


class TemporalOperator(Operator):
    def __init__(
        self,
        name: str,
        subscribes: List[str],
        window_seconds: float,
        model_path: str,
        objects: Dict[int, str],
        actions: Dict[int, str],
    ):
        super().__init__(name, subscribes, window_seconds)
        if not model_path:
            raise ValueError(f"model_path is required for {self.__class__.__name__}")
        self.model_path = model_path
        self.num_objects = len(objects)
        self.num_actions = len(actions)

        self._object_id_to_name = objects
        self._action_id_to_name = actions

        self._object_name_to_id = {v: k for k, v in objects.items()}
        self._action_name_to_id = {v: k for k, v in actions.items()}

        import torch
        self._model: torch.nn.Module = None
        self._model_load_lock = threading.Lock()
        self._load_failed = False
        self._device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

    def _object_id(self, object_name: str) -> int:
        return self._object_name_to_id.get(object_name, -1)
    
    def _object_name(self, object_id: int) -> str:
        return self._object_id_to_name.get(object_id, f"object_{object_id}")

    def _action_id(self, action_name: str) -> int:
        return self._action_name_to_id.get(action_name, -1)
    
    def _action_name(self, action_id: int) -> str:
        return self._action_id_to_name.get(action_id, f"action_{action_id}")

    def _try_load_model(self) -> bool:
        """惰性加载时序模型 （首次推理时触发，双重检查锁保证线程安全）。"""
        if self._model is not None:
            return True
        if self._load_failed:
            return False
        with self._model_load_lock:
            if self._model is not None:
                return True
            if self._load_failed:
                return False
            try:
                from pathlib import Path
                import torch

                if not Path(self.model_path).exists():
                    raise FileNotFoundError(f"模型文件不存在: {self.model_path}")
                logger.info("[%s] Loading model: %s", self.name, self.model_path)
                self._model = torch.jit.load(self.model_path, map_location=self._device)
                self._model.to(self._device)
                self._model.eval()
                logger.info("[%s] Model loaded successfully on %s", self.name, self._device)
                return True
            except Exception as e:
                self._load_failed = True
                logger.error("[%s] Model loading failed: %s", self.name, e, exc_info=True)
                raise

    def infer(self, features: "torch.Tensor") -> Optional["torch.Tensor"]:
        """时序模型推理：返回每个时间步的预测类别。"""
        import torch
        if not self._try_load_model():
            return None

        features = features.to(self._device)

        with torch.no_grad():
            logits = self._model(features)

        return logits