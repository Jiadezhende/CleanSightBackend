"""
告警服务：批量告警信息上报 + 存储目录 TTL 清理

HLS 落盘不在本服务：写侧是 `app.services.recording` → `app.storage.hls`。

本 `__init__` 不做 re-export（规范 §3 的「门面型」：只有 docstring + lifespan()），消费方走深路径：

    单例      from app.services.alarm.instance import alarm_service
    类        from app.services.alarm.service import AlarmService
    数据形状  from app.services.alarm.types import AlarmReportTask
    配置      from app.services.alarm.config import AlarmServiceConfig, get_alarm_config
"""

from contextlib import asynccontextmanager

__all__ = ["lifespan"]


@asynccontextmanager
async def lifespan():
    """alarm 服务生命周期（起于 inference 之前、停于 inference 之后）。

    在 main.py 中嵌套于 inference.lifespan 外层：inference.stop() 产出的结算告警
    仍落到仍在跑的 alarm 服务，再由此 finally 停 alarm 服务抽干队列——保序、不丢尾。

    单例 import 写在函数体内（规范 §3）。
    """
    from .instance import alarm_service

    alarm_service.start()
    try:
        yield
    finally:
        alarm_service.stop(timeout=10.0)
