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
    task_id: Optional[int] = None
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


class FrameSegment(BaseModel):
    """
    多帧数据段，包含客户端ID、任务ID、时间段
    """
    client_id: str
    task_id: Optional[int] = None
    segment_start_ts: datetime
    segment_end_ts: datetime

# SQLAlchemy models for database storage
from sqlalchemy import Column, String, Integer, DateTime
from app.database import Base

class HLSSegment(Base):
    __tablename__ = "hls_segments"

    id = Column(Integer, primary_key=True, autoincrement=True)
    client_id = Column(String, index=True)
    task_id = Column(Integer, index=True)  # 匹配 DBTask.task_id (str)
    segment_path = Column(String)  # 文件系统路径，如 /database/client_1/task_123/hls/segment_001.mp4
    playlist_path = Column(String)  # M3U8 文件路径
    start_ts = Column(DateTime)
    end_ts = Column(DateTime)
    created_at = Column(DateTime, default=datetime.now)
