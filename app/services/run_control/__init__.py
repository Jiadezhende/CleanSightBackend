"""运行控制：跨服务编排一次 run 的启停（控制面唯一编排出口）。

本 `__init__` 不做 re-export，消费方走深路径：

    单例      from app.services.run_control.instance import run_control_service
    类        from app.services.run_control.service import RunControlService
"""

from contextlib import asynccontextmanager


@asynccontextmanager
async def lifespan():
    """run_control 生命周期（无启动段，只在停机时拆掉仍在跑的 run）。

    在 `app/main.py` 里嵌在 `inference.lifespan` **里层**：先于 `inference.stop()` 拆 run，
    残段与结算告警交给此刻仍活着的 recording / alarm 队列。

    单例 import 写在函数体内（规范 §3）。
    """
    from .instance import run_control_service

    try:
        yield
    finally:
        run_control_service.shutdown()
