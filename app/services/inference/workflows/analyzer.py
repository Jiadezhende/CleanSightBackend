"""TemporalAnalyzer — 时序分析基类（L3：只产事实）

每个 Client 独立实例化一个 TemporalAnalyzer，运行于 ClientTemporalActor 的时序线程（1Hz）。

职责边界（分层改造后）：
- L3 时序分析层「只产事实」（EventFact / SegmentFact），不做告警判定。
- 告警判定迁出到 L4 规则层（Judge，见 judge.py）。

内部接口（实时与离线共用同一套）：
- load(task_id)        : 把落盘特征重读回内存（仅离线链路需要；实时不调用）。预留。
- trans(frames)        : 上游聚合后特征 → 时序模型特征空间。
- infer(feats)         : 前向（纯逻辑状态机推进 或 神经网络）；游标 last_ts 在此推进。
- post_process(raw, ts, online): 产出后端声明的 dataclass。
                          online=True  → List[EventFact]（实时滑窗打点）
                          online=False → List[SegmentFact]（离线全序列分段，Phase 2）
- run(window, online)  : 串起 trans→infer→post_process 的唯一入口。

状态归属：
- 检测/测量状态机 self._sm（tracker / 计数 / 去抖游标）是 Analyzer 的内部成员，由子类初始化。
- 决策状态（上升沿锁存、结算比较）归 Judge，不在这里。
- 同一实例不跨 Client 共享（每个 Client set_task() 时新建）。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Union

from app.services.inference.data_models import DetectionOutput, EventFact, SegmentFact

logger = logging.getLogger(__name__)


class TemporalAnalyzer(ABC):
    """有状态时序分析器基类（只产事实）。

    约束：
    - self.name 必须与配对 Detector.name 一致，用于 slide_window 查找与 Judge 配对。
    - self._sm 由子类 __init__ 负责初始化（测量状态机）。
    - 每个 Client 独立实例，不跨 Client 共享。
    """

    def __init__(self, name: str, subscribes: Optional[List[str]] = None):
        self.name = name
        # 订阅的上游 source（默认只订阅自身 name）。预留：未来 stage 级跨 source 分析。
        self.subscribes = subscribes or [name]
        self._sm: Dict[str, Any] = {}  # 子类 __init__ 负责完整初始化
        self._feature_store = None     # 离线链路注入；实时链路保持 None

    # ── 离线链路预留（实时链路不调用）──────────────────────────
    def attach_feature_store(self, feature_store) -> None:
        """注入 FeatureStore，供离线链路 load() 回读全序列特征。

        实时链路不调用；离线编排 worker（后续）在 run(online=False) 前调用。
        """
        self._feature_store = feature_store

    def load(self, task_id: Any) -> List[DetectionOutput]:
        """从落盘特征回读本 source 全序列（仅离线链路需要）。

        需先 attach_feature_store()；否则视为未实现（实时链路不应走到这里）。
        子类可 override 实现自定义回读 / 特征对齐。
        """
        if self._feature_store is None:
            raise NotImplementedError(
                f"{type(self).__name__}.load() 需先 attach_feature_store() 注入 FeatureStore"
            )
        return self._feature_store.load(task_id, source=self.name)

    # ── 时序处理三段式 ────────────────────────────────────────
    @abstractmethod
    def trans(self, frames: List[DetectionOutput]) -> Any:
        """上游聚合后特征 → 时序模型特征空间。"""

    @abstractmethod
    def infer(self, feats: Any) -> Any:
        """前向：纯逻辑状态机推进 或 神经网络。游标 last_ts 在此推进。"""

    @abstractmethod
    def post_process(
        self, raw: Any, ts: float, online: bool
    ) -> List[Union[EventFact, SegmentFact]]:
        """产出事实 dataclass。online→EventFact（打点）/ offline→SegmentFact（分段）。"""

    def run(
        self, window: List[DetectionOutput], online: bool = True
    ) -> List[Union[EventFact, SegmentFact]]:
        """唯一入口：trans → infer → post_process。

        Args:
            window: 滑动窗口快照（DetectionOutput 列表，按时间升序）。
                    实时由 cq.get_slide_window(self.name) 提供；
                    离线由 self.load(task_id) 回读的全序列提供。
            online: True=实时滑窗（产 EventFact）；False=离线全序列（产 SegmentFact）。

        Returns:
            事实列表（EventFact 或 SegmentFact）。空窗口返回 []。
        """
        if not window:
            return []
        return self.post_process(self.infer(self.trans(window)), window[-1].timestamp, online)
