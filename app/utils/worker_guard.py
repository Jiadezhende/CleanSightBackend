"""worker_guard.py - Worker 线程自愈包装器。

与 GuardedExecutor（函数级重试）互补：
- GuardedExecutor：重试单次函数调用（如 persist_segment()）
- guarded_run：重启 worker 的整个主循环（如 while not stop_event）

覆盖场景：主循环控制逻辑的意外异常、C 扩展引发的 Python 级异常。
不覆盖：C 级 segfault、死锁（属 Service 级故障，需人工介入）。

用法::

    thread = threading.Thread(
        target=guarded_run,
        args=(self._run, self._stop_event, "TemporalActor-xxx"),
        daemon=True,
    )
"""

import logging
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)


def guarded_run(
    target: Callable[[], None],
    stop_event: threading.Event,
    name: str,
    max_restarts: int = 3,
    cooldown: float = 2.0,
) -> None:
    """包裹 worker 主循环，崩溃时自动重启。

    Args:
        target: worker 主循环函数（正常退出 = stop_event 已触发）
        stop_event: 停止信号（已 set 时不再重启）
        name: worker 名称（日志标识）
        max_restarts: 最大连续重启次数
        cooldown: 重启前等待秒数
    """
    restarts = 0
    while not stop_event.is_set():
        try:
            target()
            return  # 正常退出
        except Exception:
            restarts += 1
            if restarts <= max_restarts and not stop_event.is_set():
                logger.error(
                    "[%s] Worker crashed (%d/%d), restarting in %.1fs",
                    name, restarts, max_restarts, cooldown,
                    exc_info=True,
                )
                time.sleep(cooldown)
            else:
                logger.critical(
                    "[%s] Worker terminated after %d crash(es), not restarting",
                    name, restarts,
                    exc_info=True,
                )
                return