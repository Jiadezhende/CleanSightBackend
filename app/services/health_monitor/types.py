"""健康监控相关数据类型"""

from dataclasses import dataclass


@dataclass
class ReconnectState:
    """重连状态"""
    client_id: str
    stream_url: str
    fps: int
    protocol: str
    attempt_count: int
    last_attempt_time: float
    last_frame_time_before_disconnect: float
