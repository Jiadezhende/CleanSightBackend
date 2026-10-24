"""健康监控相关数据类型"""

from dataclasses import dataclass


@dataclass(frozen=True)
class ReconnectState:
    """一次 run 的重连状态（存于该 run 的 RECONNECT_STATE topic；改字段 = replace 后重新 set）。"""

    task_id: int   # 运行键（路由标识）
    stream_url: str
    last_attempt_time: float            # 上次 respawn 时刻（reconnect_interval 节流基准）
    last_frame_time_before_disconnect: float   # 进入重连时的最后帧 ts（判「有新帧到达」的冻结基准）
