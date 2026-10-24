"""一次 run 的实时告警账本：冷却闸门 + seq + 最近 100 条环形日志（前端按 seq 增量轮询）。

    ledger = alarm_sink.ledger_for(sess)
    ledger.record(alarm, mode) -> bool       # 过闸才赋 seq、入日志
    ledger.snapshot(since_seq) -> (alarms, max_seq)
    ledger.recent(n) -> alarms

由 `alarm_sink` 按 `Session` 弱引用挂表（session/mq 提案 A9：实时告警是过渡功能，账本从 CQ 原样平移）。
本类不认识 run 状态：「CLOSED 后不再入账」由 sink 在调用前判。
"""

import threading
import time
from collections import deque
from typing import Deque, Dict, List, Tuple

from app.types.alarm import Alarm


class AlarmLedger:
    def __init__(self, gate_window: float = 5.0, capacity: int = 100):
        self._lock = threading.Lock()   # L2：_log + _seq + _gate
        self._log: Deque[Alarm] = deque(maxlen=capacity)
        self._seq = 0
        self._gate: Dict[str, float] = {}
        self._gate_window = gate_window

    def record(self, alarm: Alarm, mode: str) -> bool:
        """闸门去重 + 入环形日志，锁内原子完成。

        True = 已记录（赋 seq 并入日志），False = 被冷却窗口拦截、未记录。
        闸门按 (alarm.metric, mode) 限流，固定窗口从上次放行起算（被拦截的不续期）。
        """
        gate_key = f"{alarm.metric}:{mode}"
        now = time.time()
        with self._lock:
            last = self._gate.get(gate_key)
            if last is not None and (now - last) < self._gate_window:
                return False
            self._gate[gate_key] = now
            self._seq += 1
            alarm.seq = self._seq
            self._log.append(alarm)
            return True

    def recent(self, n: int = 10) -> List[Alarm]:
        """最近 n 条（最新在后）。"""
        with self._lock:
            items = list(self._log)
        return items[-n:]

    def snapshot(self, since_seq: int = 0) -> Tuple[List[Alarm], int]:
        """原子返回 (seq > since_seq 的增量, max_seq)。

        同锁读保证 max_seq ≥ 返回告警的最大 seq，调用方用推进后的 since_seq 不会跳过未返回的告警。
        """
        with self._lock:
            return [a for a in self._log if a.seq > since_seq], self._seq
