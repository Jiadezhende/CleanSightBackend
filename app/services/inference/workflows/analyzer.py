"""TemporalAnalyzer — 实时时序分析基类（L3：只产事实）

每个 Client 独立实例化一个 TemporalAnalyzer，运行于 ClientTemporalActor 的时序线程（1Hz）。

职责边界：
- L3 时序分析层「只产事实」（EventFact 打点），不做告警判定。
- 告警判定迁出到 L4 规则层（Judge，见 judge.py）。
- 纯实时链路：因果滑窗 → EventFact。离线链路（全序列 → SegmentFact）已彻底分离，
  不在本基类；待离线 worker 接入时另立 OfflineAnalyzer，仅共享数据契约不共享行为。

内部接口（三段式）：
- trans(frames)         : 上游聚合后特征 → 时序模型特征空间。
- infer(feats)          : 前向（纯逻辑状态机推进 或 神经网络）；游标 last_ts 在此推进。
- post_process(raw, ts) : 产出 List[EventFact]（实时滑窗打点）。
- run(window)           : 串起 trans→infer→post_process 的唯一入口。

状态归属：
- 检测/测量状态机 self._sm（tracker / 计数 / 去抖游标）是 Analyzer 的内部成员，由子类初始化。
- 决策状态（上升沿锁存、结算比较）归 Judge，不在这里。
- 同一实例不跨 Client 共享（每个 Client set_task() 时新建）。
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app.services.inference.data_models import DetectionOutput, EventFact

logger = logging.getLogger(__name__)


class TemporalAnalyzer(ABC):
    """有状态实时时序分析器基类（只产 EventFact）。

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

    # ── 时序处理三段式 ────────────────────────────────────────
    @abstractmethod
    def trans(self, frames: List[DetectionOutput]) -> Any:
        """上游聚合后特征 → 时序模型特征空间。"""

    @abstractmethod
    def infer(self, feats: Any) -> Any:
        """前向：纯逻辑状态机推进 或 神经网络。游标 last_ts 在此推进。"""

    @abstractmethod
    def post_process(self, raw: Any, ts: float) -> List[EventFact]:
        """产出实时打点事实 List[EventFact]。"""

    def run(self, window: List[DetectionOutput]) -> List[EventFact]:
        """唯一入口：trans → infer → post_process。

        Args:
            window: 滑动窗口快照（DetectionOutput 列表，按时间升序），
                    由 cq.get_slide_window(self.name) 提供。

        Returns:
            List[EventFact]。空窗口返回 []。
        """
        if not window:
            return []
        return self.post_process(self.infer(self.trans(window)), window[-1].timestamp)
