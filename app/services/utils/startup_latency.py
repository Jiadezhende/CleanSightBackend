"""启动延迟埋点：一次 run 从请求到各「首个产出」的耗时，打一行 `[startup]`。

    log_milestone("first_frame", task_id=1, step_id=2, started_at=sess.info.started_at)

「是不是首个」由生产方自己判断（每个里程碑单一生产者），本函数只负责打日志。
专用 logger `app.startup_latency`，便于 grep / 单独调级。`started_at <= 0`（裸建单测）不打。
"""

import logging
import time

_logger = logging.getLogger("app.startup_latency")


def log_milestone(name: str, *, task_id, step_id, started_at: float) -> None:
    if started_at <= 0.0:
        return
    _logger.info(
        "[startup] task=%s step=%s %s +%.0fms",
        task_id, step_id, name, (time.time() - started_at) * 1000.0,
    )
