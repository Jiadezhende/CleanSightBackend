"""三种存储语义：`Latest`（只留最新值）/ `Fifo`（有界队列）/ `TimeWindow`（按数据时间保留）。

    READY = Topic("READY", lambda: Fifo(maxlen=30, on_full=Fifo.REJECT))
    sess.channel(READY).push(frame)        # -> bool，非 ACTIVE / 满拒 返回 False
    sess.channel(READY).pop()              # -> Optional[item]

硬约束：
- 写（push / set）只在 session ACTIVE 时放行，检查与写入同在 session 锁内；读、取走、清空任何状态放行。
- **按时间的操作只读元素自带的 `ts`（数据时间），不读墙钟**：`Fifo.drain(until_ts=…)`、
  `TimeWindow` 的淘汰、压力日志的队头年龄。元素须有 `ts` 属性（epoch 秒）。
- `TimeWindow` 淘汰的前提是元素按 `ts` 非递减追加；乱序元素会滞留到下一次追加才被淘汰。
- 丢帧计数（Prometheus）与压力日志在 session 锁外上报。
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, Generic, List, Optional, Protocol, TypeVar

from app.services.utils.metrics import frame_drop_total
from app.services.utils.pressure import (
    DEFAULT_HIGH_WATERMARK_RATIO,
    REASON_QUEUE_HIGH_WATERMARK,
    PressureReporter,
)
from .session import Session, SessionState

T = TypeVar("T")


class Timestamped(Protocol):
    """按时间操作的元素约束：`ts` 为数据时间（epoch 秒）。"""

    @property
    def ts(self) -> float: ...


TS = TypeVar("TS", bound=Timestamped)


class Channel:
    """channel 基类：绑定到一个 session，借用它的锁与状态。"""

    _sess: Session

    def _bind(self, sess: Session) -> None:
        self._sess = sess
        self._lock = sess._lock

    def _writable(self) -> bool:
        """须在 session 锁内调用。"""
        return self._sess._state is SessionState.ACTIVE

    def _release_locked(self) -> None:
        """close 时释放数据（session 锁内调用）。"""

    def _after_seal(self) -> None:
        """seal / close 之后的收尾（session 锁外调用）。"""


class Latest(Channel, Generic[T]):
    """只留最新值。`clear()` 回到 `default`，任何状态放行。"""

    def __init__(self, default: Optional[T] = None):
        self._default = default
        self._value: Optional[T] = default

    def set(self, value: T) -> bool:
        with self._lock:
            if not self._writable():
                return False
            self._value = value
            return True

    def get(self) -> Optional[T]:
        with self._lock:
            return self._value

    def clear(self) -> None:
        with self._lock:
            self._value = self._default

    def _release_locked(self) -> None:
        self._value = self._default


@dataclass(frozen=True)
class PressureSpec:
    """`[PRESSURE]` 日志的组件名、资源名与水位（深度 ≥ watermark 视为积压）。

    watermark 缺省 = 容量 × DEFAULT_HIGH_WATERMARK_RATIO。identity（task / step / stage）取自 session。
    """

    component: str
    resource: str
    watermark: Optional[int] = None


class Fifo(Channel, Generic[T]):
    """有界先进先出。满时 `REJECT` 拒新、`EVICT` 挤掉最旧，两者都计一次丢帧。

    Args:
        maxlen: 容量（> 0）。
        on_full: `Fifo.REJECT` / `Fifo.EVICT`。
        drop_reason: 给了就在每次丢帧时 `frame_drop_total{reason=…}` +1。
        pressure: 给了就在每次 push 后喂一次压力快照。
    """

    REJECT = "reject"
    EVICT = "evict"

    def __init__(
        self,
        maxlen: int,
        *,
        on_full: str = EVICT,
        drop_reason: Optional[str] = None,
        pressure: Optional[PressureSpec] = None,
    ):
        if maxlen <= 0:
            raise ValueError(f"maxlen must be > 0, got {maxlen}")
        if on_full not in (self.REJECT, self.EVICT):
            raise ValueError(f"on_full must be REJECT or EVICT, got {on_full!r}")
        self.maxlen = maxlen
        self._on_full = on_full
        self._drop_reason = drop_reason
        self._pressure = pressure
        self._watermark = max(1, (
            pressure.watermark if pressure and pressure.watermark is not None
            else int(maxlen * DEFAULT_HIGH_WATERMARK_RATIO)
        ))
        self._reporter: Optional[PressureReporter] = None
        self._q: Deque[T] = deque()
        self._drops = 0

    def _bind(self, sess: Session) -> None:
        super()._bind(sess)
        if self._pressure is not None:
            run = sess.info.run
            self._reporter = PressureReporter(
                self._pressure.component, self._pressure.resource,
                identity={
                    "task_id": run.task_id if run else None,
                    "step_id": run.step_id if run else None,
                    "stage": sess.info.stage,
                },
            )

    def push(self, item: T) -> bool:
        """追加；返回 item 是否入队（非 ACTIVE 或满拒为 False）。"""
        with self._lock:
            if not self._writable():
                return False
            full = len(self._q) >= self.maxlen
            if full:
                self._drops += 1
                if self._on_full == self.EVICT:
                    self._q.popleft()
            accepted = not (full and self._on_full == self.REJECT)
            if accepted:
                self._q.append(item)
            depth = len(self._q)
            drops = self._drops
            oldest_ts = self._q[0].ts if self._reporter and depth >= self._watermark else None
        if full and self._drop_reason:
            frame_drop_total.labels(reason=self._drop_reason).inc()
        if self._reporter is not None:
            self._report(depth, oldest_ts, drops)
        return accepted

    def pop(self) -> Optional[T]:
        """取走队头；空则 None。"""
        with self._lock:
            return self._q.popleft() if self._q else None

    def take(self, n: int) -> Optional[List[T]]:
        """攒够 n 个则原子取走队头 n 个，否则 None。"""
        with self._lock:
            if len(self._q) < n:
                return None
            return [self._q.popleft() for _ in range(n)]

    def drain(self, until_ts: Optional[float] = None) -> List[T]:
        """原子取走：`until_ts=None` 全部；否则队头 `ts <= until_ts` 的连续前缀。"""
        with self._lock:
            if until_ts is None:
                items = list(self._q)
                self._q.clear()
                return items
            taken: List[T] = []
            while self._q and self._q[0].ts <= until_ts:
                taken.append(self._q.popleft())
            return taken

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)

    @property
    def drops(self) -> int:
        """累计丢帧数（满拒 + 满挤）。"""
        return self._drops

    def _report(self, depth: int, oldest_ts: Optional[float], drops: int) -> None:
        self._reporter.observe(
            depth >= self._watermark,
            reason=REASON_QUEUE_HIGH_WATERMARK,
            depth=depth,
            capacity=self.maxlen,
            utilization=depth / self.maxlen,
            oldest_age_ms=((time.time() - oldest_ts) * 1000.0) if oldest_ts is not None else None,
            drop_total=drops,
        )

    def _release_locked(self) -> None:
        self._q.clear()

    def _after_seal(self) -> None:
        if self._reporter is not None:
            self._reporter.reset()


class TimeWindow(Channel, Generic[TS]):
    """按数据时间保留：追加后淘汰 `ts < 新元素.ts - span` 的队头。

    `span` 只增不减（`extend_span`），供使用方在 run 开始时按需放宽保留时长。
    """

    def __init__(self, span: float):
        self._span = float(span)
        self._q: Deque[TS] = deque()

    def push(self, item: TS) -> bool:
        with self._lock:
            if not self._writable():
                return False
            self._q.append(item)
            cutoff = item.ts - self._span
            while self._q and self._q[0].ts < cutoff:
                self._q.popleft()
            return True

    def snapshot(self) -> List[TS]:
        with self._lock:
            return list(self._q)

    def extend_span(self, span: float) -> None:
        with self._lock:
            self._span = max(self._span, float(span))

    @property
    def span(self) -> float:
        return self._span

    def __len__(self) -> int:
        with self._lock:
            return len(self._q)

    def _release_locked(self) -> None:
        self._q.clear()


__all__ = ["Channel", "Latest", "Fifo", "PressureSpec", "TimeWindow", "Timestamped"]
