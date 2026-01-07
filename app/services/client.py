"""
容器，管理每个客户端的队列。提供客户端数据存取接口。
"""

from collections import deque
from typing import Deque, Optional, List
from app.models.frame import FrameData
from app.models.task import Task as CleaningTask


class ClientQueues:
    """
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

    # --- 封装操作方法，减少外部直接操作队列 ---
    def append_ca_ready(self, frame_data: FrameData) -> None:
        self.ca_ready.append(frame_data)

    def append_ca_raw(self, frame_data: FrameData) -> None:
        self.ca_raw.append(frame_data)

    def append_ca_processed(self, frame_data: FrameData) -> None:
        self.ca_processed.append(frame_data)
        # 同步更新 latest_processed 以便快速访问
        self.latest_processed = frame_data

    def append_rt_processed(self, frame_data: FrameData) -> None:
        self.rt_processed.append(frame_data)

    def get_latest_result(self) -> Optional[FrameData]:
        """返回最新的实时结果（优先 RT 队列，否则返回 latest_processed）。"""
        if self.rt_processed:
            return self.rt_processed[-1]
        return self.latest_processed

    def get_task_id(self) -> Optional[int]:
        return self.task.task_id if self.task else None

    def to_status_dict(self) -> dict:
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "rt_processed": len(self.rt_processed),
            "rtmp_url": self.rtmp_url,
        }

    def has_enough_for_segment(self, seg_len: int) -> bool:
        return len(self.ca_raw) >= seg_len and len(self.ca_processed) >= seg_len

    def pop_n_ca_raw(self, n: int) -> List[FrameData]:
        out: List[FrameData] = []
        for _ in range(min(n, len(self.ca_raw))):
            out.append(self.ca_raw.popleft())
        return out

    def pop_n_ca_processed(self, n: int) -> List[FrameData]:
        out: List[FrameData] = []
        for _ in range(min(n, len(self.ca_processed))):
            out.append(self.ca_processed.popleft())
        # 更新 latest_processed 为队列中最后一项（若存在），否则保持原值
        if self.ca_processed:
            self.latest_processed = self.ca_processed[-1]
        return out

    def clear(self) -> None:
        self.ca_ready.clear()
        self.ca_raw.clear()
        self.ca_processed.clear()
        self.rt_processed.clear()
        self.latest_processed = None
        self.task = None
        self.rtmp_url = None

    def pop_ca_ready(self) -> Optional[FrameData]:
        return self.ca_ready.popleft() if self.ca_ready else None

    def pop_n_ca_ready(self, n: int) -> List[FrameData]:
        out: List[FrameData] = []
        for _ in range(min(n, len(self.ca_ready))):
            out.append(self.ca_ready.popleft())
        return out

    def set_task(self, task: Optional[CleaningTask]) -> None:
        self.task = task

    def get_task(self) -> Optional[CleaningTask]:
        return self.task