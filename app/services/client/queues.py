"""
客户端队列管理
"""

import threading
import time
from collections import deque
from typing import Deque, List, Optional

import numpy as np

from app.models.frame import FrameData
from app.models.task import Task as CleaningTask

from .state import ClientState


class ClientQueues:
    """
    架构说明：
    - CA-ReadyQueue: 从 RTMP 提取的原始帧，等待推理（设置最大长度防止溢出）
    - CA-RawQueue: 原始帧副本，用于生成原始视频 HLS 段（设置最大长度防止溢出）
    - CA-ProcessedQueue: 推理后的处理帧（含关键点），用于生成处理后 HLS 段（设置最大长度防止溢出）
    - RT-ProcessedQueue: 实时推理结果（含关键点），用于 WebSocket 推送（约1秒缓存）

    内存保护：
    - 所有队列都设置了 maxlen 限制，当队列满时自动丢弃最旧的帧
    - 默认 CA 队列最大长度为 2700 帧，约 90 秒的视频缓存（30fps）
    - RT 队列长度约为 1 秒的帧数，用于实时推送

    新增功能（优化版）：
    - 支持帧率降频控制（inference_fps）
    - 支持统一 resize 尺寸配置
    - 支持直接存储 np.ndarray 原始帧
    """

    def __init__(
        self,
        client_id: str = "",
        rt_maxlen: int = 30,
        ca_segment_len: int = 150,
        ca_maxlen: int = 2700,
        resize_width: int = 640,
        resize_height: int = 480,
        inference_fps: int = 15,
        initial_stage: str = "LEAK",
    ):
        # 客户端标识
        self.client_id = client_id

        # 尺寸配置
        self.resize_width = resize_width
        self.resize_height = resize_height

        # 推理帧率配置
        self.inference_fps = inference_fps
        self.last_inference_timestamp: float = 0.0

        # 线程锁（保护时间戳更新和最新帧访问）
        self._lock = threading.Lock()

        # 最新原始帧缓存（用于异步聚合可视化）
        self.latest_raw_frame: Optional[np.ndarray] = None
        self.latest_raw_timestamp: float = (
            time.time()
        )  # 初始化为创建时间，支持启动失败检测

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
        self.stream_url: Optional[str] = None  # RTMP 流地址

        # 业务状态管理（新增）
        self.state = ClientState(client_id=client_id, initial_stage=initial_stage)

    # --- 封装操作方法，减少外部直接操作队列 ---
    def append_ca_ready_with_throttle(self, frame_data: FrameData) -> bool:
        """
        添加帧到待推理队列（带帧率限制）
        用于拉流时降频写入，避免频繁推理

        Args:
            frame_data: 帧数据

        Returns:
            True 表示写入成功，False 表示跳过（帧率限制或队列满）
        """
        with self._lock:
            # 帧率控制：间隔 = 1.0 / inference_fps
            current_time = time.time()
            interval = 1.0 / self.inference_fps

            if current_time - self.last_inference_timestamp < interval:
                return False  # 跳过，不写入

            # 检查队列是否满（maxlen 可能为 None）
            max_len = self.ca_ready.maxlen
            if max_len is not None and len(self.ca_ready) >= max_len:
                return False  # 队列满，丢弃

            self.ca_ready.append(frame_data)
            self.last_inference_timestamp = current_time
            return True

    def append_ca_raw(self, frame_data: FrameData) -> bool:
        """
        添加原始帧到落盘队列，同时更新最新原始帧缓存

        Returns:
            True 表示成功，False 表示队列已满
        """
        try:
            self.ca_raw.append(frame_data)
            # 同步更新最新原始帧（用于异步聚合可视化）
            with self._lock:
                self.latest_raw_frame = frame_data.frame
                self.latest_raw_timestamp = frame_data.timestamp
            return True
        except Exception:
            return False

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

    def get_latest_frame(self) -> Optional[np.ndarray]:
        """获取最新原始帧（用于可视化）。

        由于推理降帧，可视化需要使用最新的原始帧而非推理时的旧帧。

        Returns:
            最新原始帧（np.ndarray）或 None（如果没有帧）
        """
        with self._lock:
            if self.latest_raw_frame is not None:
                return self.latest_raw_frame.copy()
            return None

    def get_task_id(self) -> Optional[int]:
        return self.task.task_id if self.task else None

    def to_status_dict(self) -> dict:
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "rt_processed": len(self.rt_processed),
            "rtmp_url": self.stream_url,
        }

    def get_queue_depths(self) -> dict:
        """
        获取队列深度统计（新增方法，与 to_status_dict 类似但更语义化）

        Returns:
            包含各队列长度的字典
        """
        return {
            "ca_ready": len(self.ca_ready),
            "ca_raw": len(self.ca_raw),
            "ca_processed": len(self.ca_processed),
            "rt_processed": len(self.rt_processed),
        }

    def get_ca_ready_capacity(self) -> int:
        """获取 ca_ready 队列的最大容量

        Returns:
            队列最大长度，如果未设置则返回 0
        """
        return self.ca_ready.maxlen or 0

    def get_ca_raw_capacity(self) -> int:
        """获取 ca_raw 队列的最大容量

        Returns:
            队列最大长度，如果未设置则返回 0
        """
        return self.ca_raw.maxlen or 0

    def get_ca_raw_length(self) -> int:
        """获取 ca_raw 队列的当前长度

        Returns:
            队列当前帧数
        """
        return len(self.ca_raw)

    def get_ca_processed_length(self) -> int:
        """获取 ca_processed 队列的当前长度

        Returns:
            队列当前帧数
        """
        return len(self.ca_processed)

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
        self.stream_url = None

    def pop_ca_ready(self) -> Optional[FrameData]:
        """从推理队列弹出一帧（FIFO）
        
        Returns:
            FrameData 或 None（队列为空）
        """
        return self.ca_ready.popleft() if self.ca_ready else None

    def set_task(self, task: Optional[CleaningTask]) -> None:
        self.task = task

    def get_task(self) -> Optional[CleaningTask]:
        return self.task
