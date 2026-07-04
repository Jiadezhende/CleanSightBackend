"""健康监控相关数据类型"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ReconnectState:
    """重连状态"""

    task_id: int   # 运行键（路由标识）
    stream_url: str
    fps: int
    protocol: str
    attempt_count: int
    last_attempt_time: float
    last_frame_time_before_disconnect: float
    # 进入重连时捕获的 CQ 对象引用（对象身份 fence 基准）。每 tick 比对当前槽位 cq：
    # 不同 → 槽位已被 /start 重启换成新 run，放弃本次重连；attempt 耗尽 → stop_run(expected=cq)，
    # 防 HM 过期决策误删健康新 run。捕获点须在**进入重连**时（非 give-up 时），
    # 否则 snapshot() 拿到的可能已是换槽后的新 cq。
    cq: Any = None
