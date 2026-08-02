"""健康监控相关数据类型"""

from dataclasses import dataclass
from typing import Any


@dataclass
class ReconnectState:
    """重连状态"""

    task_id: int   # 运行键（路由标识）
    stream_url: str
    last_attempt_time: float            # 上次 respawn 时刻（reconnect_interval 节流基准）
    last_frame_time_before_disconnect: float   # 进入重连时的最后帧 ts（判「有新帧到达」的冻结基准）
    # 进入重连时捕获的 CQ 对象引用（对象身份 fence 基准）。每 tick 比对当前槽位 cq：
    # 不同 → 槽位已被 /start 重启换成新 run，放弃本次重连；无帧满 cleanup_timeout →
    # stop_run(expected=cq)，防 HM 过期决策误删健康新 run。捕获点须在**进入重连**时
    # （非 give-up 时），否则 snapshot() 拿到的可能已是换槽后的新 cq。
    cq: Any = None
