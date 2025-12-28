from app.models.frame import FrameData
from app.models.task import CleanTask as CleaningTask


from collections import deque
from typing import Deque, Optional


class ClientQueues:
    """容器，管理每个客户端的队列。

    架构说明：
    - CA-ReadyQueue: 从 RTMP 提取的原始帧，等待推理（设置最大长度防止溢出）
    - CA-RawQueue: 原始帧副本，用于生成原始视频 HLS 段（设置最大长度防止溢出）
    - CA-ProcessedQueue: 推理后的处理帧（含关键点），用于生成处理后 HLS 段（设置最大长度防止溢出）
    - RT-ProcessedQueue: 实时推理结果（含关键点），用于 WebSocket 推送（约1秒缓存）

    内存保护：
    - 所有队列都设置了 maxlen 限制，当队列满时自动丢弃最旧的帧
    - 默认 CA 队列最大长度为 500 帧，约 16.7 秒的视频缓存（30fps）
    - RT 队列长度约为 1 秒的帧数，用于实时推送
    """

    def __init__(self, rt_maxlen: int, ca_segment_len: int, ca_maxlen: int = 500):
        # CA-ReadyQueue: 等待推理的原始帧（设置最大长度限制防止溢出）
        self.ca_ready: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # CA-RawQueue: 原始帧副本，用于落盘生成原始视频（设置最大长度限制）
        self.ca_raw: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # CA-ProcessedQueue: 处理后的帧，用于生成 HLS（设置最大长度限制）
        self.ca_processed: Deque[FrameData] = deque(maxlen=ca_maxlen)
        # RT-ProcessedQueue: 实时推理结果，约 1 秒缓存用于 WebSocket 推送
        self.rt_processed: Deque[FrameData] = deque(maxlen=rt_maxlen)
        self.ca_segment_len = ca_segment_len
        self.latest_processed: Optional[FrameData] = None  # 快速访问最新处理帧
        self.task: Optional[CleaningTask] = None  # 关联的清洗任务
        self.rtmp_url: Optional[str] = None  # RTMP 流地址