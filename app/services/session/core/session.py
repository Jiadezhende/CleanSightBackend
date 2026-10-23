"""`Session`：一次 run 的身份 + 状态机 + topic 字典；`Topic`：topic 声明。

    from app.services.session.core import Topic, Latest
    LATEST_X = Topic("LATEST_X", Latest)          # 使用方模块声明
    sess.channel(LATEST_X).set(x)                 # 写：只在 ACTIVE 放行
    sess.channel(LATEST_X).get()                  # 读：任何状态放行

硬约束：
- 每 session 一把锁（L2 叶子锁），护状态、topic 字典与全部 channel 数据；持锁期间不调外部代码。
- 写门与状态切换同锁：seal / close 之后的写一律被拒。
- 状态只由 run_control 经 `SessionService` 切换（seal / close），写方不调。
- channel 首次访问时按声明创建；已 CLOSED 则现造不入表的空实例（写被拒、读为空）。

设计见 `docs/update/20261022_SESSION_MQ_PROPOSAL.md`。
"""

from __future__ import annotations

import enum
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable, Dict, Generic, List, TypeVar

from app.types.run import RunIdentity

if TYPE_CHECKING:
    from .channels import Channel

C = TypeVar("C", bound="Channel")


class SessionState(enum.Enum):
    """单调推进 ACTIVE → DRAINING → CLOSED。只有 ACTIVE 可写；读与清空任何状态放行。"""

    ACTIVE = "ACTIVE"
    DRAINING = "DRAINING"
    CLOSED = "CLOSED"


@dataclass(frozen=True)
class SessionInfo:
    """不可变身份。`started_at` 为 epoch 秒（task 超时看门狗与启动埋点的参考钟）。"""

    run: RunIdentity
    source_ip: str = ""
    stage: str = ""
    started_at: float = 0.0


class Topic(Generic[C]):
    """topic 声明：名字 + 工厂。工厂零参、返回一个未绑定的 channel，每个 session 调一次。

    业务参数（容量、丢帧计数 reason、压力日志名）由声明方算好放进工厂，基建不认识它们。
    按对象身份区分：同名的两个 `Topic` 是两个 topic。
    """

    __slots__ = ("name", "_factory")

    def __init__(self, name: str, factory: Callable[[], C]):
        self.name = name
        self._factory = factory

    def __repr__(self) -> str:
        return f"Topic({self.name!r})"


class Session:
    """一次 run。由 `SessionService.open` 创建并注册；测试可直接构造（不入注册表）。"""

    def __init__(self, info: SessionInfo):
        self.info = info
        self._lock = threading.Lock()   # L2 叶子锁：状态 + topic 字典 + 全部 channel 数据
        self._state = SessionState.ACTIVE
        self._channels: Dict[Topic, "Channel"] = {}

    def __repr__(self) -> str:
        return f"Session({self.info.run}, {self._state.name})"

    # ── 状态 ──────────────────────────────────────────────

    @property
    def state(self) -> SessionState:
        """免锁读（枚举引用原子读 + 单调）；判写放行与否以 channel 内同锁检查为准。"""
        return self._state

    def is_active(self) -> bool:
        return self._state is SessionState.ACTIVE

    def seal(self) -> bool:
        """ACTIVE → DRAINING（幂等）。返回本次是否发生切换。只由 `SessionService.seal` 调。"""
        with self._lock:
            if self._state is not SessionState.ACTIVE:
                return False
            self._state = SessionState.DRAINING
            channels = list(self._channels.values())
        for ch in channels:
            ch._after_seal()
        return True

    def close(self) -> bool:
        """→ CLOSED（幂等），摘掉全部 channel 并释放数据。返回本次是否发生切换。

        只由 `SessionService.close` 调；释放前须已交出残帧（run_control 保证）。
        """
        with self._lock:
            if self._state is SessionState.CLOSED:
                return False
            self._state = SessionState.CLOSED
            channels = list(self._channels.values())
            self._channels = {}
            for ch in channels:
                ch._release_locked()
        for ch in channels:
            ch._after_seal()
        return True

    # ── channel ───────────────────────────────────────────

    def channel(self, topic: Topic[C]) -> C:
        """本 run 的 `topic` 实例；首次访问时按声明创建。

        工厂是声明方代码，在锁外调用；并发首访只有一个实例入表，其余丢弃。
        """
        with self._lock:
            ch = self._channels.get(topic)
            if ch is not None:
                return ch
        new = topic._factory()
        new._bind(self)
        with self._lock:
            if self._state is SessionState.CLOSED:
                return new
            return self._channels.setdefault(topic, new)

    def topics(self) -> List[Topic]:
        """已创建的 topic（调试用）。"""
        with self._lock:
            return list(self._channels)
