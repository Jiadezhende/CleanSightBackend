"""run 注册表：`task_id → Session`，读免锁（COW 不可变快照），写串行。

    sess = session_service.open(SessionInfo(run, source_ip, stage, started_at))
    session_service.get(task_id) / snapshot() / find_by_source_ip(ip)
    session_service.seal(sess)        # 拒写，读照常
    session_service.close(sess)       # 摘出注册表（身份核对）+ 释放全部数据

硬约束：
- 一个 task 至多一个注册中的 `Session`；`open` 遇已有注册项抛 `SessionConflictError`。
- open / seal / close 只由 run_control 持任务锁调用。
- 查表只在入口（routers、health_monitor、run_control）；写方捕获 `Session` 后只对它读写。
"""

from __future__ import annotations

import logging
import threading
import types
from typing import Dict, Mapping, Optional

from .core import Session, SessionInfo

logger = logging.getLogger(__name__)


class SessionConflictError(RuntimeError):
    """同一 task 已有注册中的 session（换代须先走完 stop_run）。"""


class SessionService:
    def __init__(self):
        # 不可变快照：永不就地改，写时整体换引用；读者原子读引用后随意迭代。
        self._runs: Dict[int, Session] = {}
        self._wlock = threading.Lock()   # L2 叶子锁：只串行注册表写

    # ── 读（免锁）────────────────────────────────────────

    def get(self, task_id: int) -> Optional[Session]:
        return self._runs.get(task_id)

    def has(self, task_id: int) -> bool:
        return task_id in self._runs

    def snapshot(self) -> Mapping[int, Session]:
        """零拷贝只读视图（键 = task_id）。"""
        return types.MappingProxyType(self._runs)

    def find_by_source_ip(self, source_ip: str) -> Optional[Session]:
        """该点位当前的 run；同点位多个 run 取最晚启动者。"""
        latest: Optional[Session] = None
        for sess in self._runs.values():
            if sess.info.source_ip == source_ip and (
                latest is None or sess.info.started_at > latest.info.started_at
            ):
                latest = sess
        return latest

    # ── 生命周期写（run_control 持任务锁调用）─────────────

    def open(self, info: SessionInfo) -> Session:
        task_id = info.run.task_id
        sess = Session(info)
        with self._wlock:
            if task_id in self._runs:
                raise SessionConflictError(f"task {task_id} already has a registered session")
            new = dict(self._runs)
            new[task_id] = sess
            self._runs = new
        logger.info("[SessionService] open: %s", info.run)
        return sess

    def seal(self, sess: Session) -> bool:
        """ACTIVE → DRAINING。返回本次是否发生切换。"""
        return sess.seal()

    def close(self, sess: Session) -> bool:
        """注册项仍是 `sess` 才摘出；无论是否在表，`sess` 一律置 CLOSED 并释放数据。

        返回是否从注册表摘出。
        """
        task_id = sess.info.run.task_id
        removed = False
        with self._wlock:
            if self._runs.get(task_id) is sess:
                new = dict(self._runs)
                del new[task_id]
                self._runs = new
                removed = True
        sess.close()
        if removed:
            logger.info("[SessionService] close: %s", sess.info.run)
        return removed
