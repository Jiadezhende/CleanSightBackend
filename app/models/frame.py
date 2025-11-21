"""Frame data models for AI inference pipeline.

These Pydantic models represent raw and processed frames as they will be
stored (e.g., later in a database). In in-memory realtime queues we keep
numpy arrays for performance, converting to Base64 only when persisting or
returning to clients.
"""

from pydantic import BaseModel, Field
from typing import Optional, Dict, Any, List
from datetime import datetime

class BaseFrame(BaseModel):
    task_id: Optional[str] = None
    client_id: Optional[str] = None
    raw_timestamp: Optional[datetime] = None  # 原始视频帧写入时间戳
    width: Optional[int] = None
    height: Optional[int] = None
    metadata: Optional[Dict[str, Any]] = None   # Additional data (optional)


class RawFrame(BaseFrame):
    raw_frame_b64: str  # Base64 encoded original frame


class ProcessedFrame(BaseFrame):
    """
    处理后帧数据，包含Base64编码的处理后图像及推理结果
    """
    processed_frame_b64: str  # Base64 encoded processed (annotated) frame
    inference_result: Optional[Dict[str, Any]] = None  # detection / analysis output


class FrameData(BaseModel):
    """
    单帧数据，包含时间戳、原始与处理后图像的Base64编码，以及推理结果
    """
    ts: datetime
    raw_b64: str
    processed_b64: str
    inference: Dict[str, Any]


class FrameSegment(BaseModel):
    """
    多帧数据段，包含客户端ID、任务ID、时间段及对应的多帧数据
    """
    client_id: str
    task_id: Optional[str] = None
    segment_start_ts: datetime
    segment_end_ts: datetime
    frames: Dict[int, FrameData]  # key = frame_number, value = FrameData

    # Example frame dict structure:
    # {
    #   frame_number: FrameData(ts=..., raw_b64=..., processed_b64=..., inference={...})
    # }
