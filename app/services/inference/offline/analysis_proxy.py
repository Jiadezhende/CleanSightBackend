"""离线分析父进程侧 —— 子进程 spawn 与结果句柄。

    proc = spawn(task_id, step_id, run_id, threads)      # 起一个 daemon 子进程
    proc.poll_result(timeout) / proc.is_alive() / proc.kill() / proc.close()

结果通道是单向 Pipe：子进程退出即 EOF。父进程在子进程存活期间持续 poll，故长 message
不会把子进程堵在 send 上。超时 / 取消 / 停机的 kill 与收尸规则由 `service` 持有，本模块
只提供进程句柄。
"""

from __future__ import annotations

import logging
import multiprocessing
from multiprocessing.connection import Connection
from typing import Any, Dict, Optional

from .analysis_child import worker_main

logger = logging.getLogger(__name__)


class WorkerProcess:
    """一次离线作业的子进程句柄：读结果、等退出、kill 与回收。"""

    def __init__(self, proc, conn: Connection) -> None:
        self._proc = proc
        self._conn = conn
        self._result: Optional[Dict[str, Any]] = None
        self._at_eof = False
        self._closed = False

    @property
    def pid(self) -> Optional[int]:
        return self._proc.pid

    @property
    def exitcode(self) -> Optional[int]:
        return self._proc.exitcode

    @property
    def at_eof(self) -> bool:
        """结果通道已 EOF（子进程关闭写端或已退出）。"""
        return self._at_eof

    def is_alive(self) -> bool:
        return self._proc.is_alive()

    def join(self, timeout: Optional[float] = None) -> None:
        self._proc.join(timeout)

    def kill(self) -> None:
        try:
            if self._proc.is_alive():
                self._proc.kill()
        except (OSError, ValueError):  # 已退出 / 句柄已关
            pass

    def poll_result(self, timeout: float) -> Optional[Dict[str, Any]]:
        """最多等 timeout 秒取结果：取到后重复调用返回同一份；超时 / EOF / 非 dict → None。"""
        if self._result is not None:
            return self._result
        if self._at_eof:
            return None
        try:
            if not self._conn.poll(timeout):
                return None
            payload = self._conn.recv()
        except (EOFError, OSError):
            self._at_eof = True
            return None
        self._result = payload if isinstance(payload, dict) else None
        return self._result

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._conn.close()
        except Exception:  # pragma: no cover - 关闭失败无后续影响
            pass
        try:
            if not self._proc.is_alive():
                self._proc.close()
        except (OSError, ValueError):  # pragma: no cover - 已关 / 仍在跑
            pass


def spawn(
    task_id: int, step_id: int, run_id: Optional[int], threads: int,
) -> WorkerProcess:
    """起一个 spawn 子进程跑一次 run；父进程只留读端（子进程退出即 EOF）。"""
    ctx = multiprocessing.get_context("spawn")
    recv_conn, send_conn = ctx.Pipe(duplex=False)
    proc = ctx.Process(
        target=worker_main,
        args=(send_conn, task_id, step_id, run_id, threads),
        name=f"OfflineWorker-{task_id}/{step_id}/{run_id}",
        daemon=True,
    )
    try:
        proc.start()
    except BaseException:
        recv_conn.close()
        send_conn.close()
        raise
    # 父进程不能留着写端：否则子进程退出后父进程永远读不到 EOF。
    send_conn.close()
    return WorkerProcess(proc, recv_conn)
